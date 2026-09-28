"""图谱检索读取层 — 从 knowledge.db 图谱表读取并做访问过滤后扩展检索候选.

图谱不是全量文本检索的替代，而是"实体/关系/元数据检索扩展"：
1. 识别查询中出现的图谱实体名（module / submodule / error_code / role / process…）；
2. 候选节点/边按归属文档执行访问过滤（先过滤后输出：粗过滤 WHERE JOIN
   documents 当前授权列 + 逐条 decide() 精判定），未授权节点的名字、边、来源
   与元数据一律不返回；
3. 命中文档的证据 chunk 形成有序扩展候选（供 RRF 与 BM25/向量融合）。

安全约定：节点/边的 meta_json 只是构建时描述快照；访问判定永远以
documents 表当前列为准（本模块在输出前 JOIN documents 校验）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

_GRAPH_TABLES = ("graph_nodes", "graph_edges")

#: 图谱实体类型中"可作为检索扩展信号"的类型（article 文档本身由归属文档覆盖）
_SIGNAL_TYPES = ("department", "module", "submodule", "process",
                 "error_code", "role")


@dataclass
class GraphNodeHit:
    name: str
    node_type: str
    document_id: str
    description: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class GraphEdgeHit:
    predicate: str
    source: str            # f"{type}:{name}"
    target: str
    document_id: str
    evidence_chunk_id: str = ""
    description: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class GraphExpansion:
    """一次图谱扩展的完整结果（所有元素已经过访问过滤）。

    过滤审计字段（filtered_nodes / filtered_edges / filter_reasons）只包含
    计数与机器可读 reason 码，绝不包含被拒节点/边的名字或关系——用于
    retrieval_trace 如实报告"为何被权限过滤"而不泄漏未授权内容。
    """

    query: str
    node_hits: list[GraphNodeHit] = field(default_factory=list)
    edge_hits: list[GraphEdgeHit] = field(default_factory=list)
    expanded_chunk_ids: list[str] = field(default_factory=list)
    matched_documents: list[str] = field(default_factory=list)
    #: 已授权扩展 chunk → 归属文档（供 trace 展示候选 document/chunk id）
    chunk_documents: dict[str, str] = field(default_factory=dict)
    #: 被访问判定拒绝的候选节点/边计数（只计数，不含名字/关系）
    filtered_nodes: int = 0
    filtered_edges: int = 0
    filter_reasons: dict[str, int] = field(default_factory=dict)

    def note_filter(self, reason: str | None, *, nodes: int = 0, edges: int = 0) -> None:
        """记录一次权限过滤（聚合 reason 码；不记录被拒内容）。"""
        if nodes:
            self.filtered_nodes += nodes
        if edges:
            self.filtered_edges += edges
        key = reason or "UNSPECIFIED"
        self.filter_reasons[key] = self.filter_reasons.get(key, 0) + max(nodes + edges, 1)

    @property
    def hits(self) -> int:
        return len(self.expanded_chunk_ids)

    @property
    def nodes(self) -> int:
        return len(self.node_hits)

    @property
    def edges(self) -> int:
        return len(self.edge_hits)


def _document_access_where(conn: sqlite3.Connection, access_filter,
                           prefix: str = "d.") -> tuple[str, tuple]:
    """构建 documents 访问谓词（与 access.filter 契约一致，纯参数化）。

    access_filter 为 None → 仅排除归档文档。
    documents 表缺授权列（legacy）→ status-only 回退（legacy 语料视为公共）。
    """
    if access_filter is None:
        return f"{prefix}status != 'archived'", ()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    if "classification" not in cols:
        return f"{prefix}status = ?", (access_filter.status,)
    from access.filter import build_document_access_predicate
    clause, params = build_document_access_predicate(access_filter, prefix=prefix)
    return clause, params


def _document_meta(conn: sqlite3.Connection, document_id: str) -> dict | None:
    """读取文档当前授权列（访问判定的单一事实源）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    if "classification" in cols:
        row = conn.execute(
            "SELECT classification, project_id, business_domain, status "
            "FROM documents WHERE document_id = ?", (document_id,),
        ).fetchone()
        if row is None:
            return None
        return {"classification": row[0], "project_id": row[1],
                "business_domain": row[2], "status": row[3]}
    row = conn.execute(
        "SELECT status FROM documents WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return None
    return {"classification": "public", "project_id": None,
            "business_domain": None, "status": row[0]}


def _document_decision(conn: sqlite3.Connection, access_context,
                       document_id: str) -> tuple[bool, str | None]:
    """访问判定 + 机器可读 reason 码（reason 仅用于聚合审计，不含内容）。

    access_context 为 None → 允许（仅 coarse 过滤，与既有行为一致）。
    """
    if access_context is None:
        return True, None
    meta = _document_meta(conn, document_id)
    if meta is None:
        return False, "RESOURCE_MISSING"
    if meta.get("status") != "active":
        return False, "RESOURCE_INACTIVE"
    from access import ResourceMetadata, decide
    from access.actions import Action
    decision = decide(
        access_context,
        Action.INTERNAL_RETRIEVAL_ASK,
        ResourceMetadata(
            classification=meta["classification"], status=meta["status"],
            project_id=meta["project_id"], business_domain=meta["business_domain"],
        ),
    )
    return decision.allowed, decision.reason


def _node_ref_score(query: str, name: str, node_type: str) -> int:
    """确定性的节点匹配分：只在实体名完整出现在查询中时命中（避免噪声过召回）。

    - 大小写不敏感的子串包含（含错误码 FC-ERR-2032、模块名、角色名等）；
    - 返回 0 = 不命中；分值反映强信号类型优先（module/error_code 最高）。
    """
    if not name or not query:
        return 0
    if name.lower() not in query.lower():
        return 0
    base = {  # 类型优先级（module/error_code 语义最精确）
        "module": 4, "error_code": 4, "role": 3,
        "process": 3, "submodule": 3, "department": 2,
    }
    if node_type == "article":
        return 1
    return base.get(node_type, 1)


class GraphReader:
    """只读图谱读取器。在给定 sqlite 连接上工作（调用方负责只读连接生命周期）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── 状态 ───────────────────────────────────────────────

    def status(self) -> dict:
        """图谱组件状态（文件/表缺失、空库 → enabled=False + error 原因）。"""
        try:
            tables = {r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.OperationalError:
            return self._off("表不可读")
        missing = [t for t in _GRAPH_TABLES if t not in tables]
        if missing:
            return self._off(f"缺少图谱表: {missing}")
        try:
            nodes = self.conn.execute(
                "SELECT COUNT(*) FROM graph_nodes g "
                "JOIN documents d ON d.document_id = g.document_id "
                "WHERE d.status != 'archived'").fetchone()[0]
            edges = self.conn.execute(
                "SELECT COUNT(*) FROM graph_edges e "
                "JOIN documents d ON d.document_id = e.document_id "
                "WHERE d.status != 'archived'").fetchone()[0]
        except sqlite3.OperationalError as e:
            return self._off(f"图谱读取失败: {e}")
        if nodes == 0 or edges == 0:
            return self._off("图谱为空（未构建图数据）")
        return {
            "enabled": True, "nodes": nodes, "edges": edges,
            "error": None,
        }

    @staticmethod
    def _off(reason: str) -> dict:
        return {"enabled": False, "nodes": 0, "edges": 0, "error": reason}

    # ── 扩展 ───────────────────────────────────────────────

    def expand(self, query: str, *, top_k: int = 10,
               access_filter=None, access_context=None,
               max_candidates: int = 40) -> GraphExpansion:
        """查询 → 节点命中 → 文档访问过滤 → 扩展证据 chunk（有序）。

        返回的 node_hits / edge_hits / expanded_chunk_ids / matched_documents
        全部来自调用方有权限访问的文档；未授权内容不会出现在任何字段里。
        """
        exp = GraphExpansion(query=query)
        where, params = _document_access_where(self.conn, access_filter)
        rows = self.conn.execute(
            "SELECT g.node_id, g.name, g.node_type, g.document_id, "
            "g.description, g.meta_json, d.status AS d_status "
            "FROM graph_nodes g JOIN documents d ON d.document_id = g.document_id "
            f"WHERE {where}",
            params,
        ).fetchall()

        # 1) 节点命中（含类型优先级打分）
        scored: list[tuple[int, str, dict]] = []
        for r in rows:
            s = _node_ref_score(query, r["name"], r["node_type"])
            if s > 0:
                allowed, reason = _document_decision(
                    self.conn, access_context, r["document_id"])
                if not allowed:
                    exp.note_filter(reason, nodes=1)
                    continue
                scored.append((s, r["document_id"], dict(r)))
        scored.sort(key=lambda t: (-t[0], t[1]))
        allowed_docs: list[str] = []
        node_hits: list[GraphNodeHit] = []
        for s, doc_id, r in scored:
            if doc_id not in allowed_docs:
                allowed_docs.append(doc_id)
            if len(node_hits) < max_candidates:
                node_hits.append(GraphNodeHit(
                    name=r["name"], node_type=r["node_type"],
                    document_id=r["document_id"],
                    description=r["description"],
                    meta=json.loads(r["meta_json"] or "{}"),
                ))
        if not allowed_docs:
            return exp

        # 2) 命中文档内相关边（仅返回有权文档的边）
        exp.matched_documents = allowed_docs
        exp.node_hits = node_hits[:max_candidates]
        ph = ",".join("?" * len(allowed_docs))
        edge_rows = self.conn.execute(
            "SELECT e.predicate, e.evidence_chunk_id, e.description, "
            "e.meta_json, e.document_id, "
            "n1.name AS s_name, n1.node_type AS s_type, "
            "n2.name AS t_name, n2.node_type AS t_type "
            f"FROM graph_edges e "
            "JOIN graph_nodes n1 ON n1.node_id = e.source_node_id "
            "JOIN graph_nodes n2 ON n2.node_id = e.target_node_id "
            f"WHERE e.document_id IN ({ph}) "
            "ORDER BY e.document_id, e.predicate",
            allowed_docs,
        ).fetchall()
        for e in edge_rows:
            allowed, reason = _document_decision(
                self.conn, access_context, e["document_id"])
            if not allowed:
                exp.note_filter(reason, edges=1)
                continue
            exp.edge_hits.append(GraphEdgeHit(
                predicate=e["predicate"],
                source=f"{e['s_type']}:{e['s_name']}",
                target=f"{e['t_type']}:{e['t_name']}",
                document_id=e["document_id"],
                evidence_chunk_id=e["evidence_chunk_id"],
                description=e["description"],
                meta=json.loads(e["meta_json"] or "{}"),
            ))

        # 3) 扩展证据 chunk：按命中文档顺序取各文档 active 的 chunk（单一证据块）
        #    （当前语料一篇文档恰一个 chunk；多 chunk 场景按 seq 顺序取前 N。）
        #    顺序 = 文档命中分数排序（强信号在前），保证 RRF 的 graph rank 可解释。
        cap = max(top_k * 4, 20)
        per_doc = {}
        ph2 = ",".join("?" * len(allowed_docs))
        chunk_rows = self.conn.execute(
            "SELECT c.chunk_id, c.document_id FROM chunks c "
            f"WHERE c.document_id IN ({ph2}) AND c.status != 'archived' "
            "ORDER BY c.document_id, c.seq",
            allowed_docs,
        ).fetchall()
        for c in chunk_rows:
            per_doc.setdefault(c["document_id"], []).append(c["chunk_id"])
        for doc_id in allowed_docs:
            for cid in per_doc.get(doc_id, []):
                exp.expanded_chunk_ids.append(cid)
                exp.chunk_documents[cid] = doc_id
                if len(exp.expanded_chunk_ids) >= cap:
                    return exp
        return exp
