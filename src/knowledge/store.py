"""SQLite 检索库 — 精确检索（SQL LIKE）+ 全文检索（FTS5）基线.

表结构:
  documents  — 文档元数据 + 授权元数据（classification/project_id/business_domain/status，
               块检索继承授权的单一事实源；chunks 不复制这些字段）
  chunks     — 结构优先切分后的文本块（含出处列）
  chunks_fts — FTS5 虚拟表（chunk_id + searchable 词元流）
  entities   — 命名实体
  relations  — 显式关系 + 证据字段

默认排除 status='archived' 的文档；任何结果都带
document_id / page / version / status 出处 + 文档级授权元数据。
新库直接含授权列；旧库在打开时经 migrations.migrate_schema 幂等补列。
"""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import (
    CLASSIFICATIONS, Chunk, Document, Relation, SearchResult, GraphNode, GraphEdge,
    LEGACY_DEPARTMENT_CONTRACT, derive_access_scope,
    normalize_applicable_departments,
)
from .migrations import (
    CONTENT_CONTRACT_TABLE_DDL, migrate_schema, migrate_content_contract,
    upgrade_retrieval_schema, GRAPH_TABLE_DDL,
)
from .chunking import chunk_document
from .textutils import searchable, fts_match_query, overlap_score, query_terms_weighted
from .metadata import finalize_meta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id    TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    version        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    classification TEXT NOT NULL DEFAULT 'public',
    access_scope   TEXT NOT NULL DEFAULT 'company_public',
    project_id     TEXT,
    business_domain TEXT NOT NULL DEFAULT 'research',
    department     TEXT NOT NULL DEFAULT '',
    owner_department        TEXT NOT NULL DEFAULT '',
    business_category       TEXT NOT NULL DEFAULT '',
    applicable_departments_json TEXT NOT NULL DEFAULT '[]',
    owner          TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    pages          INTEGER NOT NULL DEFAULT 1,
    created        TEXT NOT NULL DEFAULT '',
    updated        TEXT NOT NULL DEFAULT '',
    metadata_json  TEXT NOT NULL DEFAULT '{}',
    submitter_id   TEXT NOT NULL DEFAULT '',
    reviewer_id    TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT NOT NULL DEFAULT '',
    reviewed_at    TEXT NOT NULL DEFAULT '',
    published_at   TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL DEFAULT '',
    approval_note  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL,
    page         INTEGER NOT NULL DEFAULT 1,
    version      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active',
    heading_path TEXT NOT NULL DEFAULT '',
    seq          INTEGER NOT NULL DEFAULT 0,
    text         TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE TABLE IF NOT EXISTS entities (
    name        TEXT PRIMARY KEY,
    type        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS relations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_id           TEXT NOT NULL DEFAULT '',
    document_id       TEXT NOT NULL DEFAULT '',
    subject           TEXT NOT NULL,
    predicate         TEXT NOT NULL,
    object            TEXT NOT NULL,
    evidence_chunk_ids TEXT NOT NULL DEFAULT '[]',
    evidence_page     INTEGER NOT NULL DEFAULT 1,
    evidence_quote    TEXT NOT NULL DEFAULT '',
    evidence_class    TEXT NOT NULL DEFAULT '',
    extraction_rule   TEXT NOT NULL DEFAULT '',
    confidence        REAL,
    evidence_section  TEXT NOT NULL DEFAULT '',
    evidence_offset   INTEGER,
    properties_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_relations_edge_id
ON relations(edge_id) WHERE edge_id <> '';
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    searchable
);
""" + GRAPH_TABLE_DDL + CONTENT_CONTRACT_TABLE_DDL

#: 块检索结果需要 JOIN 继承的 documents 列（授权列 + 适用范围列 + 文档元数据）。
#: 集中一处声明，避免各检索方法重复列清单而漂移。
_DOC_META_COLUMNS = (
    "d.title, d.classification, d.access_scope, d.project_id, d.business_domain, "
    "d.department, d.owner_department, d.business_category, "
    "d.applicable_departments_json, d.metadata_json AS doc_meta_json"
)


class KnowledgeStore:
    """SQLite 检索库。db_path 缺省为内存库（测试友好）。"""

    def __init__(self, db_path=None, chunk_max_chars: int | None = None):
        self.db_path = db_path
        self.chunk_max_chars = chunk_max_chars
        if db_path is not None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path or ":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        # 旧库幂等补授权列/索引（新库无操作）
        migrate_schema(self.conn)
        # 旧库幂等补内容适用范围列 + 适用部门索引表（新库无操作；不做重回填）
        migrate_content_contract(self.conn)
        # 旧库幂等补 chunk 继承元数据列 + 图谱表 + 回填（新库无操作）
        upgrade_retrieval_schema(self.conn)

    def close(self):
        self.conn.close()

    # ── 写入 ──────────────────────────────────────────────

    def ingest(self, doc: Document) -> int:
        """写入/覆盖一份文档（含切块、实体、关系）。返回块数。"""
        doc.status = doc.status if doc.status in ("draft", "pending_review", "active", "rejected", "archived") else "draft"
        doc.classification = doc.classification if doc.classification in CLASSIFICATIONS else "public"
        doc.project_id = str(doc.project_id) if doc.project_id else None
        if not doc.business_domain:
            doc.business_domain = "research"
        # 授权范围键：显式值优先，否则由分类/项目派生（与授权列保持一致）。
        doc.access_scope = doc.access_scope or derive_access_scope(
            doc.classification, doc.project_id)
        # 内容适用范围：显式值优先，否则按旧 department 标签映射补齐（不臆造）；
        # 一律归一为去重保序的部门注册表键列表（授权无关）。
        legacy = LEGACY_DEPARTMENT_CONTRACT.get(doc.department or "", {})
        if not doc.owner_department:
            doc.owner_department = legacy.get("owner_department", "")
        if not doc.business_category:
            doc.business_category = legacy.get("business_category", "")
        if not doc.applicable_departments:
            doc.applicable_departments = list(
                legacy.get("applicable_departments", ()))
        doc.applicable_departments = normalize_applicable_departments(
            doc.applicable_departments)
        self.conn.execute(
            """INSERT OR REPLACE INTO documents
               (document_id, title, version, status, classification, access_scope,
                project_id, business_domain, department, owner_department,
                business_category, applicable_departments_json,
                owner, source, pages, created, updated, metadata_json,
                submitter_id, reviewer_id, submitted_at, reviewed_at,
                published_at, content_hash, approval_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc.document_id, doc.title, doc.version, doc.status,
             doc.classification, doc.access_scope, doc.project_id,
             doc.business_domain, doc.department, doc.owner_department,
             doc.business_category,
             json.dumps(doc.applicable_departments, ensure_ascii=False),
             doc.owner, doc.source, doc.pages, doc.created,
             doc.updated, json.dumps(doc.metadata, ensure_ascii=False),
             doc.submitter_id, doc.reviewer_id, doc.submitted_at, doc.reviewed_at,
             doc.published_at, doc.content_hash, doc.approval_note),
        )
        self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc.document_id,))
        self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id LIKE ?", (f"{doc.document_id}:%",))
        self.conn.execute("DELETE FROM relations WHERE document_id = ?", (doc.document_id,))
        self.conn.execute("DELETE FROM graph_nodes WHERE document_id = ?", (doc.document_id,))
        self.conn.execute("DELETE FROM graph_edges WHERE document_id = ?", (doc.document_id,))
        # 适用部门规范化索引行（供检索前按部门键过滤）
        self.conn.execute(
            "DELETE FROM document_applicable_departments WHERE document_id = ?",
            (doc.document_id,))
        for dept_key in doc.applicable_departments:
            self.conn.execute(
                "INSERT OR IGNORE INTO document_applicable_departments "
                "(document_id, department_key) VALUES (?, ?)",
                (doc.document_id, dept_key))

        chunks = chunk_document(doc, max_chars=self.chunk_max_chars)
        for c in chunks:
            self._insert_chunk(c)

        for ent in doc.entities:
            self.conn.execute(
                "INSERT OR REPLACE INTO entities (name, type, description) VALUES (?,?,?)",
                (ent.name, ent.type, ent.description),
            )
        for rel in doc.relations:
            self.conn.execute(
                """INSERT INTO relations
                   (document_id, subject, predicate, object,
                    evidence_chunk_ids, evidence_page, evidence_quote)
                   VALUES (?,?,?,?,?,?,?)""",
                (doc.document_id, rel.subject, rel.predicate, rel.object,
                 json.dumps(rel.evidence_chunk_ids, ensure_ascii=False),
                 rel.evidence_page, rel.evidence_quote),
            )
        for node in doc.graph_nodes:
            self.conn.execute(
                """INSERT OR REPLACE INTO graph_nodes
                   (node_id, name, node_type, document_id, description, meta_json)
                   VALUES (?,?,?,?,?,?)""",
                (node.node_id, node.name, node.node_type, node.document_id,
                 node.description, json.dumps(node.meta, ensure_ascii=False)),
            )
        for edge in doc.graph_edges:
            self.conn.execute(
                """INSERT OR REPLACE INTO graph_edges
                   (edge_id, document_id, source_node_id, target_node_id, predicate,
                    evidence_chunk_id, description, meta_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (edge.edge_id, edge.document_id, edge.source_node_id,
                 edge.target_node_id, edge.predicate, edge.evidence_chunk_id,
                 edge.description, json.dumps(edge.meta, ensure_ascii=False)),
            )
        self.conn.commit()
        return len(chunks)

    def _insert_chunk(self, c: Chunk):
        self.conn.execute(
            """INSERT INTO chunks
               (chunk_id, document_id, page, version, status, heading_path, seq,
                text, metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (c.chunk_id, c.document_id, c.page, c.version, c.status,
             c.heading_path, c.seq, c.text,
             json.dumps(c.metadata, ensure_ascii=False)),
        )
        self.conn.execute(
            "INSERT INTO chunks_fts (chunk_id, searchable) VALUES (?,?)",
            (c.chunk_id, searchable(f"{c.heading_path}\n{c.text}")),
        )

    def ingest_many(self, docs: Iterable[Document]) -> int:
        total = 0
        for doc in docs:
            total += self.ingest(doc)
        return total

    def delete_document(self, document_id: str):
        """删除文档（含其块、FTS 行、关系、适用部门索引行）。"""
        self.conn.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
        self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        self.conn.execute("DELETE FROM chunks_fts WHERE chunk_id LIKE ?", (f"{document_id}:%",))
        self.conn.execute("DELETE FROM relations WHERE document_id = ?", (document_id,))
        self.conn.execute(
            "DELETE FROM document_applicable_departments WHERE document_id = ?",
            (document_id,))
        self.conn.commit()

    # ── 内容适用范围查询（授权无关；供后端在检索前按业务按钮过滤）──────

    def find_documents_by_applicable_department(
            self, department_key: str, *, include_archived: bool = False) -> list[str]:
        """返回“适用于给定部门键”的 document_id 列表（走规范化索引表）。

        仅做内容适用范围过滤，不做任何授权判定；授权仍由 src/access 的
        documents 谓词负责（先过滤后输出）。
        """
        sql = (
            "SELECT DISTINCT a.document_id FROM document_applicable_departments a "
            "JOIN documents d ON d.document_id = a.document_id "
            "WHERE a.department_key = ?"
        )
        if not include_archived:
            sql += " AND d.status != 'archived'"
        sql += " ORDER BY a.document_id"
        return [r[0] for r in self.conn.execute(sql, (department_key,))]

    # ── 查询 ──────────────────────────────────────────────

    def get_document(self, document_id: str, include_archived: bool = False):
        """按 document_id 返回 {出处 + 元数据}；归档文档默认不可见。"""
        sql = "SELECT * FROM documents WHERE document_id = ?"
        if not include_archived:
            sql += " AND status != 'archived'"
        row = self.conn.execute(sql, (document_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def _status_filter(self, include_archived: bool, alias: str = "c") -> str:
        return "" if include_archived else f" AND {alias}.status != 'archived'"

    def exact_search(self, query: str, top_k: int = 10,
                     include_archived: bool = False,
                     document_id: str | None = None) -> list:
        """SQL LIKE 精确检索（中文子串友好）。"""
        q = f"%{query}%"
        sql = (
            "SELECT c.*, " + _DOC_META_COLUMNS + " "
            "FROM chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            "WHERE (c.text LIKE ? OR c.heading_path LIKE ?)" +
            self._status_filter(include_archived)
        )
        args = [q, q]
        if document_id:
            sql += " AND c.document_id = ?"
            args.append(document_id)
        sql += " ORDER BY c.document_id, c.seq LIMIT ?"
        args.append(top_k)
        return [self._to_result(r, score=1.0, match_type="exact")
                for r in self.conn.execute(sql, args)]

    def fts_search(self, query: str, top_k: int = 10,
                   include_archived: bool = False,
                   document_id: str | None = None) -> list:
        """FTS5 全文检索（中英文词元 OR 召回，bm25 排序）。"""
        match = fts_match_query(query)
        if not match:
            return []
        sql = (
            "SELECT c.*, " + _DOC_META_COLUMNS + ", "
            "bm25(chunks_fts) AS rank FROM chunks_fts f "
            "JOIN chunks c ON c.chunk_id = f.chunk_id "
            "JOIN documents d ON d.document_id = c.document_id "
            "WHERE chunks_fts MATCH ?" + self._status_filter(include_archived)
        )
        args = [match]
        if document_id:
            sql += " AND c.document_id = ?"
            args.append(document_id)
        sql += " ORDER BY rank LIMIT ?"
        args.append(top_k)
        # bm25 为负值，越小越相关 → 分数取 -rank
        return [self._to_result(r, score=-r["rank"], match_type="fts")
                for r in self.conn.execute(sql, args)]

    def retrieve(self, query: str, top_k: int = 10,
                 include_archived: bool = False,
                 document_id: str | None = None) -> list:
        """合并精确 + 全文检索候选，再按查询词元重叠分排序。

        整句自然语言问题（如"SAP 项目 P001 的 2026 年采购金额是多少？"）
        用词元重叠分衡量相关度，默认排除归档文档。
        """
        exact = self.exact_search(query, max(top_k * 2, 8), include_archived, document_id)
        fts = self.fts_search(query, max(top_k * 4, 16), include_archived, document_id)
        merged = {r.chunk_id: r for r in exact}
        for r in fts:
            if r.chunk_id not in merged:
                merged[r.chunk_id] = r
            elif r.score > merged[r.chunk_id].score:
                merged[r.chunk_id].score = r.score
                merged[r.chunk_id].match_type = "exact+fts"
        if not merged:
            return []
        terms = query_terms_weighted(query)

        def _rank(res):
            # 精确命中整体加权重排；词元重叠分为主序
            return overlap_score(res.text, terms) + (2.0 if res.match_type == "exact" else 0.0)

        ranked = sorted(merged.values(), key=_rank, reverse=True)
        return ranked[:top_k]

    def get_relations(self, subject: str | None = None,
                      object: str | None = None,
                      include_archived: bool = False) -> list:
        """按主语/宾语查询显式关系（带证据）。默认排除归档文档。"""
        sql = (
            "SELECT r.*, d.title, d.version AS d_version, d.status AS d_status "
            "FROM relations r "
            "JOIN documents d ON d.document_id = r.document_id "
            "WHERE 1=1" + self._status_filter(include_archived, alias="d")
        )
        args = []
        if subject:
            sql += " AND r.subject = ?"; args.append(subject)
        if object:
            sql += " AND r.object = ?"; args.append(object)
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            out.append({
                "document_id": r["document_id"],
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
                "evidence": {
                    "chunk_ids": json.loads(r["evidence_chunk_ids"] or "[]"),
                    "page": r["evidence_page"],
                    "quote": r["evidence_quote"],
                },
                "version": r["d_version"],
                "status": r["d_status"],
                "title": r["title"],
            })
        return out

    def search_by_entity(self, entity_name: str, top_k: int = 10,
                         include_archived: bool = False) -> list:
        """实体 → 显式关系证据 → 原文证据块。

        优先用关系里的 evidence.chunk_ids；缺省时回退到
        document_id + page 取整页证据块。
        """
        relations = self.get_relations(subject=entity_name,
                                       include_archived=include_archived)
        seen, results = set(), []
        for rel in relations:
            cids = rel["evidence"]["chunk_ids"]
            if cids:
                rows = []
                for cid in cids:
                    row = self.conn.execute(
                        "SELECT c.*, " + _DOC_META_COLUMNS + " FROM chunks c "
                        "JOIN documents d ON d.document_id = c.document_id "
                        "WHERE c.chunk_id = ?", (cid,)).fetchone()
                    if row is not None:
                        rows.append(row)
            else:
                rows = self.conn.execute(
                    "SELECT c.*, " + _DOC_META_COLUMNS + " FROM chunks c "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "WHERE c.document_id = ? AND c.page = ?",
                    (rel["document_id"], rel["evidence"]["page"])).fetchall()
            for row in rows:
                if row["chunk_id"] in seen:
                    continue
                seen.add(row["chunk_id"])
                results.append(self._to_result(row, score=1.0, match_type="entity"))
                if len(results) >= top_k:
                    return results
        return results

    # ── 内部 ──────────────────────────────────────────────

    @staticmethod
    def _to_result(row, score: float, match_type: str) -> SearchResult:
        # 授权元数据继承自 documents 行（单一事实源），不复制到 chunk。
        chunk_meta = json.loads(row["metadata_json"] or "{}") if "metadata_json" in row.keys() else {}
        doc_meta = json.loads(row["doc_meta_json"] or "{}") if "doc_meta_json" in row.keys() else {}
        merged = {**doc_meta, **chunk_meta}

        def _col(name, default=""):
            return row[name] if name in row.keys() else default

        try:
            applicable_raw = json.loads(_col("applicable_departments_json", "[]"))
        except (TypeError, json.JSONDecodeError):
            applicable_raw = []
        applicable = normalize_applicable_departments(applicable_raw)
        meta = finalize_meta(
            merged,
            department=_col("department"),
            classification=row["classification"],
            access_scope=_col("access_scope", ""),
            project_id=row["project_id"],
            business_domain=row["business_domain"],
            status=row["status"],
            applicable_departments=applicable or None,
            owner_department=_col("owner_department", ""),
            business_category=_col("business_category", ""),
        )
        return SearchResult(
            chunk_id=row["chunk_id"],
            text=row["text"],
            heading_path=row["heading_path"],
            page=row["page"],
            document_id=row["document_id"],
            title=row["title"],
            version=row["version"],
            status=row["status"],
            score=score,
            match_type=match_type,
            classification=row["classification"],
            access_scope=_col("access_scope", "") or derive_access_scope(
                row["classification"], row["project_id"]),
            project_id=row["project_id"],
            business_domain=row["business_domain"],
            department=_col("department"),
            applicable_departments=applicable,
            owner_department=_col("owner_department", ""),
            business_category=_col("business_category", ""),
            meta=meta,
        )


def open_store(db_path=None) -> KnowledgeStore:
    return KnowledgeStore(db_path)
