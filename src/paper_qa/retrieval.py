"""只读知识检索 — 唯一检索入口：BM25/FTS + 可选向量 + 可选图谱 → 加权 RRF → 可选 rerank.

链路（本文件 = 产品/服务的唯一检索入口）：
  问题 → 中文词元化 → BM25/FTS 候选（SQLite FTS5）
        → 向量候选（BGE-M3/FAISS，若索引与模型可用）
        → 图谱扩展候选（GraphRAG，实体/关系/元数据，若图数据可用）
        → 加权 RRF 融合（仅对实际可用的分支计分）
        → 可选 BGE reranker 精排
        → 授权过滤（在候选进入排序/生成前完成）
        → 证据

安全降级（诚实状态）：
  - 缺模型 / FAISS / 图谱数据 / reranker → 相应组件 enabled=False 并记录原因，
    保留可用的 FTS 结果；mode 与 retrieval_info 只按"实际参与计分的分支"报告，
    绝不继续报告 hybrid_rrf 或 graph 已开启。
  - 授权集成：access_filter 在 SQL 中参数化粗过滤（未授权文档根本不进候选池）；
    access_context 对每条候选 decide() 精判定，未授权 chunk 永不进入结果。

每次调用打开独立只读连接，绝不写库，线程安全。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.graphrag_pipeline import GraphPipeline
from knowledge.hybrid_retrieval import (
    BRANCH_BM25,
    BRANCH_GRAPH,
    BRANCH_VECTOR,
    active_branches,
    contributions_present,
    match_type_for,
    rrf_fuse,
)
from knowledge.vector_index import VectorIndex, VectorIndexError, resolve_embedder
from paper_qa.rerank import (
    RerankerUnavailableError,
    dependency_status as reranker_dependency_status,
    resolve_local_model_dir,
    resolve_reranker,
)
from paper_qa.retrieval_trace import (
    RetrievalTrace,
    TraceCandidate,
    TraceFiltered,
    TraceStep,
    empty_trace,
)

from .text import fts_match_query, query_terms_weighted, overlap_score

_ROUTE = "INTERNAL_RETRIEVAL"
_COUNT_TABLES = ("documents", "chunks")
_CORPUS_LABEL = "department_demo_knowledge"

_INDEX_SUBDIR = "vector/chunks_bge_m3"
_DEFAULT_MODEL = os.environ.get("SRC_RETRIEVAL_MODEL", "BAAI/bge-m3")
_DEFAULT_RERANKER_MODEL = os.environ.get(
    "SRC_RETRIEVAL_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

DEFAULT_WEIGHTS = {"bm25": 1.0, "vector": 1.0, "graph": 0.6}
DEFAULT_K = 60


def _as_bool(value) -> bool | None:
    """解析 1/0/true/false/on/off；auto/缺省 → None（交给组件探测）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _read_bool(key: str, default: bool) -> bool:
    v = _as_bool(os.environ.get(key))
    return default if v is None else v


def _load_yaml_retrieval_cfg() -> dict:
    """读取 config.yaml 的 retrieval 段（env 优先）。仅本项目自有配置，不依赖 src.config。"""
    data: dict = {}
    here = Path(__file__).resolve()
    project_root = here.parents[1]          # src/paper_qa → 项目根
    for p in (project_root / "config.yaml", project_root / "src" / "config.yaml"):
        if p.exists():
            try:
                import yaml
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                cfg = {}
            if isinstance(cfg, dict) and isinstance(cfg.get("retrieval"), dict):
                data = cfg["retrieval"]
            break
    return data


def _env(name: str) -> str | None:
    return os.environ.get(name)


def trace_enabled() -> bool:
    """是否允许向调用方暴露完整 retrieval_trace（明确 debug 配置）。

    优先级：环境变量 SRC_RETRIEVAL_TRACE > config.yaml retrieval.trace.enabled > False。
    默认关闭：普通用户 API 响应保持简洁。
    """
    v = _as_bool(os.environ.get("SRC_RETRIEVAL_TRACE"))
    if v is not None:
        return v
    cfg = _load_yaml_retrieval_cfg()
    t = cfg.get("trace")
    if isinstance(t, dict):
        v = _as_bool(t.get("enabled"))
        if v is not None:
            return v
    return False


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    title: str
    heading_path: str
    page: int
    text: str
    score: float
    match_type: str = "fts"
    meta: dict = field(default_factory=dict)

    def to_dict(self, source_number: int | None = None) -> dict:
        d = {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "heading": self.heading_path,
            "page": self.page,
            "text": self.text,
            "score": round(self.score, 4),
            "match_type": self.match_type,
            "meta": dict(self.meta),
        }
        if source_number is not None:
            d["source"] = source_number
        return d


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    route: str = _ROUTE
    timing_ms: float = 0.0
    counts: dict = field(default_factory=dict)
    status: dict = field(default_factory=dict)
    #: 结构化检索 trace（仅 collect_trace=True 时填充；测试/debug 用）
    trace: RetrievalTrace | None = None


class ReadOnlyPaperRetriever:
    """knowledge.db 只读检索器（唯一检索入口）。

    db_path 必须指向现有知识库文件。可选注入（测试/装配）：
      - vector_index_dir: 向量索引目录（缺省 <db>/vector/chunks_bge_m3）
      - embedder: 显式 Embedder（缺省按配置懒加载；不可用则向量关闭）
      - reranker: 显式 reranker 实例（缺省按配置懒加载）
      - graph_enabled / vector_enabled / reranker_enabled: 强制开关（None=按探测）
      - weights / k: RRF 权重与参数
    """

    ROUTE = _ROUTE

    def __init__(self, db_path: str | Path, top_k: int = 5, *,
                 vector_index_dir: str | Path | None = None,
                 embedder=None, reranker=None,
                 vector_enabled: bool | None = None,
                 graph_enabled: bool | None = None,
                 reranker_enabled: bool | None = None,
                 weights: dict[str, float] | None = None,
                 rrf_k: int = DEFAULT_K):
        self.db_path = str(Path(db_path).resolve())
        self.top_k = top_k

        self._cfg = _load_yaml_retrieval_cfg()

        # 组件开关（显式注入 > env > yaml > 默认探测）
        self._vector_enabled = vector_enabled
        if self._vector_enabled is None:
            v = self._cfg.get("vector", {}).get("enabled") if isinstance(self._cfg.get("vector"), dict) else None
            self._vector_enabled = _as_bool(v)
        if self._vector_enabled is None:
            self._vector_enabled = _read_bool("SRC_RETRIEVAL_VECTOR", True)

        self._graph_enabled = graph_enabled
        if self._graph_enabled is None:
            g = self._cfg.get("graph", {}).get("enabled") if isinstance(self._cfg.get("graph"), dict) else None
            self._graph_enabled = _as_bool(g)
        if self._graph_enabled is None:
            self._graph_enabled = _read_bool("SRC_RETRIEVAL_GRAPH", True)

        self._reranker_enabled = reranker_enabled
        if self._reranker_enabled is None:
            r = self._cfg.get("reranker", {}).get("enabled") if isinstance(self._cfg.get("reranker"), dict) else None
            self._reranker_enabled = _as_bool(r)
        if self._reranker_enabled is None:
            self._reranker_enabled = _read_bool("SRC_RETRIEVAL_RERANKER", True)

        vec_cfg = self._cfg.get("vector") if isinstance(self._cfg.get("vector"), dict) else {}
        rerank_cfg = self._cfg.get("reranker") if isinstance(self._cfg.get("reranker"), dict) else {}
        self._model = _env("SRC_RETRIEVAL_MODEL") or vec_cfg.get("model") or _DEFAULT_MODEL
        self._backend = _env("SRC_RETRIEVAL_BACKEND") or vec_cfg.get("backend", "auto")
        self._device = _env("SRC_RETRIEVAL_DEVICE") or vec_cfg.get("device", "cpu")
        self._reranker_model = (_env("SRC_RETRIEVAL_RERANKER_MODEL")
                                or rerank_cfg.get("model") or _DEFAULT_RERANKER_MODEL)

        # RRF 参数
        w = dict(DEFAULT_WEIGHTS)
        if isinstance(self._cfg.get("rrf"), dict):
            rrf = self._cfg["rrf"]
            for b in (BRANCH_BM25, BRANCH_VECTOR, BRANCH_GRAPH):
                if b in rrf:
                    w[b] = float(rrf[b])
        if weights:
            w.update(weights)
        self.weights = w
        k = rrf_k
        if isinstance(self._cfg.get("rrf"), dict) and "k" in self._cfg["rrf"]:
            k = int(self._cfg["rrf"]["k"])
        self.rrf_k = k

        # 注入组件
        self._embedder = embedder
        self._reranker_instance = reranker
        if vector_index_dir is None:
            vector_index_dir = str(Path(self.db_path).parent / _INDEX_SUBDIR)
        self.vector_index_dir = str(Path(vector_index_dir).resolve())

        # 懒加载缓存
        self._vector_index: VectorIndex | None = None
        self._vector_error: str | None = None
        self._active_vector_used = False
        self._graph_error: str | None = None
        self._reranker_error: str | None = None
        self._reranker_attempted = False
        self._last_rerank_ms: float | None = None
        # 每次 search 后更新：实际激活分支 / 模式
        self._active_branches: list[str] = ["bm25"]
        self._reranked = False

    # ── 连接 ──────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def counts(self) -> dict:
        """知识库各表行数；缺表返回 -1（不抛错）。"""
        conn = self._connect()
        try:
            out: dict[str, int] = {}
            for t in _COUNT_TABLES:
                try:
                    out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.OperationalError:
                    out[t] = -1
            return out
        finally:
            conn.close()

    # ── 组件状态 ──────────────────────────────────────────

    def _vector_status(self) -> dict:
        """向量组件状态：配置/索引文件/维度/激活与错误原因。"""
        st = {
            "configured": self._vector_enabled,
            "active": bool(self._active_vector_used),
            "index_ready": False,
            "chunks_indexed": 0,
            "backend": self._backend,
            "model": self._model,
            "index_path": self.vector_index_dir,
            "error": None,
        }
        if self._vector_error:
            st.update({"error": self._vector_error, "active": False})
            return st
        index_dir = Path(self.vector_index_dir)
        meta_path = index_dir / "meta.json"
        if not self._vector_enabled:
            st["error"] = "配置关闭（SRC_RETRIEVAL_VECTOR/retrieval.vector.enabled=false）"
            return st
        if not meta_path.exists() or not (index_dir / "index.faiss").exists():
            st["error"] = "向量索引缺失（未构建 data/vector/chunks_bge_m3）"
            return st
        try:
            sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            st["index_ready"] = True
            st["chunks_indexed"] = int(sidecar.get("size", 0))
            st["dim"] = sidecar.get("embedder", {}).get("dim")
            st["fingerprint"] = sidecar.get("embedder", {}).get("fingerprint")
        except Exception as e:
            st["error"] = f"向量索引 meta.json 不可读: {e}"
            st["active"] = False
            return st
        return st

    def _graph_status(self) -> dict:
        if not self._graph_enabled:
            return {"enabled": False, "nodes": 0, "edges": 0,
                    "error": "配置关闭（SRC_RETRIEVAL_GRAPH/retrieval.graph.enabled=false）"}
        try:
            conn = self._connect()
            try:
                status = GraphPipeline(conn).status()
                if not status.get("enabled") and self._graph_error:
                    status = dict(status)
                    status["error"] = status.get("error") or self._graph_error
                return status
            finally:
                conn.close()
        except Exception as e:
            return {"enabled": False, "nodes": 0, "edges": 0, "error": str(e)}

    def _reranker_probe(self) -> dict:
        """reranker 本地可用性探测（不加载模型、不下载）：路径/权重/依赖。"""
        try:
            resolved = resolve_local_model_dir(self._reranker_model)
        except Exception:  # noqa: BLE001 - 探测失败按不可用处理
            resolved = None
        return {
            "resolved_path": resolved,
            "weights_present": resolved is not None,
            "dependency": reranker_dependency_status(),
        }

    def _reranker_status(self) -> dict:
        """reranker 真实状态：配置开关 / 模型路径与权重 / 依赖 / 加载结果 /
        关闭原因 / 最后一次 rerank 耗时。绝不虚报已启用。"""
        probe = self._reranker_probe()
        loaded = self._reranker_instance is not None
        if not self._reranker_enabled:
            disabled_reason = "配置关闭（SRC_RETRIEVAL_RERANKER/retrieval.reranker.enabled=false）"
            load_result = "disabled"
        elif self._reranker_error:
            disabled_reason = self._reranker_error
            load_result = "failed"
        elif loaded:
            disabled_reason = None
            load_result = "loaded"
        elif not probe["weights_present"]:
            disabled_reason = ("本地无 reranker 权重：模型名既非本地目录，本地 HF 缓存也无"
                               "可用快照；离线环境不下载，安全降级为不启用")
            load_result = "unavailable_precheck"
        elif not probe["dependency"]["installed"]:
            disabled_reason = probe["dependency"]["hint"]
            load_result = "dependency_missing"
        else:
            disabled_reason = None
            load_result = "not_attempted"
        return {
            "configured": self._reranker_enabled,
            "enabled": bool(self._reranker_enabled and disabled_reason is None),
            "active": self._reranked,
            "backend": "bge-reranker-v2-m3",
            "model": self._reranker_model,
            "model_path": self._reranker_model,
            "resolved_path": probe["resolved_path"],
            "weights_present": probe["weights_present"],
            "dependency": probe["dependency"],
            "load_attempted": self._reranker_attempted,
            "loaded": loaded,
            "load_result": load_result,
            "load_error": self._reranker_error,
            "disabled_reason": disabled_reason,
            "last_rerank_ms": self._last_rerank_ms,
            "error": disabled_reason,
        }

    def retrieval_info(self) -> dict:
        """检索状态：模式 = 实际参与计分的分支（绝不虚报 hybrid_rrf/graph）。"""
        actives = list(self._active_branches)
        graph = self._graph_status()
        vector = self._vector_status()
        reranker = self._reranker_status()

        if len(actives) >= 2:
            names = {"bm25": "bm25", "vector": "vector", "graph": "graph"}
            mode = "+".join(names[b] for b in ("bm25", "vector", "graph")
                            if b in actives) + "_rrf"
            ranking = ("weighted_rrf(k=%d, bm25=%.2f, vector=%.2f, graph=%.2f)"
                       % (self.rrf_k, self.weights.get("bm25", 0),
                          self.weights.get("vector", 0), self.weights.get("graph", 0)))
            if self._reranked:
                ranking += " then_rerank"
        else:
            mode = "sqlite_fts5_zh"
            ranking = "bm25_then_term_overlap"

        return {
            "mode": mode,
            "tokenizer": "unigram+bigram",
            "ranking": ranking,
            "corpus": _CORPUS_LABEL,
            "components": {
                "bm25": {"enabled": True, "backend": "sqlite_fts5"},
                "vector": vector,
                "graph": graph,
                "reranker": reranker,
            },
            "fusion": "weighted_rrf" if len(actives) >= 2 else "none",
            "rrf_weights": {k: self.weights.get(k) for k in ("bm25", "vector", "graph")},
            "rrf_k": self.rrf_k,
            "reranked": self._reranked,
        }

    # ── 检索 ──────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5,
               access_filter=None, access_context=None,
               collect_trace: bool = False) -> RetrievalResult:
        """检索命中的 chunks。空/无词元查询返回空结果。

        collect_trace=True 时在结果里附带结构化 retrieval_trace（测试/debug 用）。
        trace 只包含"已通过访问判定"的候选 id/分数；被权限过滤的候选仅以聚合
        计数 + reason 码出现，绝不泄漏未授权正文或图谱节点关系。
        """
        t0 = time.monotonic()
        trace: RetrievalTrace | None = None
        # 每次检索重置"实际启用"状态，避免上一次结果污染本次状态报告。
        self._reranked = False
        self._active_vector_used = False
        conn = self._connect()
        try:
            match = fts_match_query(query)
            if not match or top_k <= 0:
                res = self._empty_result(query, top_k, t0, conn)
                if collect_trace:
                    res.trace = empty_trace(query)
                return res
            access_clause, access_params = self._document_access_clause(
                conn, access_filter, prefix="d.")
            cand_n = max(top_k * 8, 40)
            if collect_trace:
                trace = RetrievalTrace(query=query)

            # ── Step 1: FTS5/BM25 候选（词元 + 分数）──────────────────
            s1 = time.monotonic()
            bm25 = self._bm25_candidates(conn, query, cand_n,
                                         access_clause=access_clause,
                                         access_params=access_params)
            bm25_filtered = TraceFiltered()
            if access_context is not None:
                kept = []
                for c in bm25:
                    allowed, reason = self._chunk_decision(
                        conn, access_context, c.document_id)
                    if allowed:
                        kept.append(c)
                    else:
                        bm25_filtered.add(reason)
                bm25 = kept
            branches: dict[str, list[dict]] = {
                BRANCH_BM25: [{"chunk_id": c.chunk_id} for c in bm25],
            }
            if trace is not None:
                trace.steps.append(TraceStep(
                    name="fts_bm25",
                    enabled=True,
                    elapsed_ms=(time.monotonic() - s1) * 1000,
                    query=query,
                    candidates=[
                        TraceCandidate(chunk_id=c.chunk_id, document_id=c.document_id,
                                       rank=i + 1, score=c.score,
                                       detail={"match_type": c.match_type})
                        for i, c in enumerate(bm25)
                    ],
                    filtered=bm25_filtered,
                    detail={
                        "tokens": [{"term": t, "weight": w}
                                   for t, w in query_terms_weighted(query)],
                        "match": match,
                        "candidate_pool": cand_n,
                        "coarse_access_filter": access_clause is not None,
                        "backend": "sqlite_fts5",
                    },
                ))

            # ── Step 2: 向量候选（索引/相似度）────────────────────────
            s2 = time.monotonic()
            vhits: list[dict] = []
            vcands: list[TraceCandidate] = []
            vfiltered = TraceFiltered()
            if self._vector_enabled:
                try:
                    idx, embedder = self._ensure_vector(conn)
                    if idx is not None:
                        hits = idx.search(query, top_k=cand_n)
                        for h in hits:
                            doc_id = (h.metadata or {}).get("document_id", "")
                            if not doc_id:
                                continue
                            if access_context is not None:
                                allowed, reason = self._chunk_decision(
                                    conn, access_context, doc_id)
                                if not allowed:
                                    vfiltered.add(reason)
                                    continue
                            vhits.append({"chunk_id": h.chunk_id})
                            vcands.append(TraceCandidate(
                                chunk_id=h.chunk_id, document_id=doc_id,
                                rank=len(vhits), score=h.score,
                                detail={"index_row": h.row}))
                        if vhits:
                            self._active_vector_used = True
                except (VectorIndexError, ImportError, OSError, RuntimeError,
                        ValueError) as e:
                    self._disable_vector(f"{type(e).__name__}: {e}")
            branches[BRANCH_VECTOR] = vhits
            if trace is not None:
                vstatus = self._vector_status()
                trace.steps.append(TraceStep(
                    name="vector",
                    enabled=bool(vhits),
                    elapsed_ms=(time.monotonic() - s2) * 1000,
                    query=query,
                    candidates=vcands,
                    filtered=vfiltered,
                    detail={
                        "configured": bool(self._vector_enabled),
                        "index_ready": vstatus.get("index_ready", False),
                        "index_path": self.vector_index_dir,
                        "model": self._model,
                        "backend": self._backend,
                        "chunks_indexed": vstatus.get("chunks_indexed", 0),
                        "dim": vstatus.get("dim"),
                        "fingerprint": vstatus.get("fingerprint"),
                        "permission": "per-candidate decide()",
                    },
                    error=vstatus.get("error"),
                ))

            # ── Step 3: GraphRAG 节点边扩展 + 元数据过滤 ──────────────
            s3 = time.monotonic()
            ghits: list[dict] = []
            gcands: list[TraceCandidate] = []
            gfiltered = TraceFiltered()
            graph_detail: dict = {}
            graph_error: str | None = None
            if self._graph_enabled:
                try:
                    expansion = GraphPipeline(conn).expand(
                        query, top_k=top_k, access_filter=access_filter,
                        access_context=access_context)
                    ghits = [{"chunk_id": cid} for cid in expansion.expanded_chunk_ids]
                    for cid in expansion.expanded_chunk_ids:
                        gcands.append(TraceCandidate(
                            chunk_id=cid,
                            document_id=expansion.chunk_documents.get(cid, ""),
                            rank=len(gcands) + 1))
                    gfiltered = TraceFiltered(
                        count=expansion.filtered_nodes + expansion.filtered_edges,
                        reasons=dict(expansion.filter_reasons))
                    graph_detail = {
                        "nodes": expansion.nodes,
                        "edges": expansion.edges,
                        "matched_documents": len(expansion.matched_documents),
                        "expanded_chunks": len(expansion.expanded_chunk_ids),
                        "coarse_access_filter": access_filter is not None,
                        "metadata_filter": ("documents.status + classification/project_id/"
                                            "business_domain + decide()"),
                    }
                except Exception as e:  # noqa: BLE001 - 图谱故障只降级，不影响 FTS
                    self._graph_error = str(e)
                    graph_error = str(e)
            branches[BRANCH_GRAPH] = ghits
            if trace is not None:
                gstatus = self._graph_status()
                trace.steps.append(TraceStep(
                    name="graph",
                    enabled=bool(ghits),
                    elapsed_ms=(time.monotonic() - s3) * 1000,
                    query=query,
                    candidates=gcands,
                    filtered=gfiltered,
                    detail={
                        "configured": bool(self._graph_enabled),
                        "available": bool(gstatus.get("enabled")),
                        "data_nodes": gstatus.get("nodes", 0),
                        "data_edges": gstatus.get("edges", 0),
                        **graph_detail,
                    },
                    error=graph_error,
                ))

            # ── Step 4: 加权 RRF 融合贡献 ─────────────────────────────
            s4 = time.monotonic()
            self._active_branches = active_branches(branches, self.weights)
            fused = rrf_fuse(branches, self.weights, self.rrf_k)
            if trace is not None:
                trace.steps.append(TraceStep(
                    name="rrf_fusion",
                    enabled=len(self._active_branches) >= 2,
                    elapsed_ms=(time.monotonic() - s4) * 1000,
                    query=query,
                    candidates=[
                        TraceCandidate(
                            chunk_id=f.chunk_id, rank=i + 1, score=f.fused_score,
                            detail={
                                "branch_ranks": dict(f.branch_ranks),
                                "branch_contributions": {
                                    b: round(v, 8)
                                    for b, v in f.branch_contributions.items()},
                                "contributing_branches": contributions_present(
                                    f.branch_contributions),
                            })
                        for i, f in enumerate(fused)
                    ],
                    filtered=TraceFiltered(),
                    detail={
                        "active_branches": list(self._active_branches),
                        "weights": {b: self.weights.get(b)
                                    for b in (BRANCH_BM25, BRANCH_VECTOR, BRANCH_GRAPH)},
                        "k": self.rrf_k,
                        "fusion": ("weighted_rrf" if len(self._active_branches) >= 2
                                   else "none"),
                    },
                ))

            # 解析候选块详情（bm25 结果里已有文本；其它分支按 chunk_id 补齐）
            by_id = {c.chunk_id: c for c in bm25}
            missing = [f.chunk_id for f in fused if f.chunk_id not in by_id]
            if missing:
                by_id.update(self._fetch_chunks(conn, missing))

            results: list[RetrievedChunk] = []
            for f in fused:
                src = by_id.get(f.chunk_id)
                if src is None:
                    continue
                meta = src.meta or {}
                results.append(RetrievedChunk(
                    chunk_id=f.chunk_id,
                    document_id=src.document_id,
                    title=src.title,
                    heading_path=src.heading_path,
                    page=src.page,
                    text=src.text,
                    score=round(f.fused_score, 6),
                    match_type=match_type_for(f.branch_contributions),
                    meta=meta,
                ))

            # ── Step 5: reranker 前后顺序与分数 ───────────────────────
            before_order = [c.chunk_id for c in results]
            rerank_scores_by_id: dict[str, float] = {}
            rerank_error: str | None = None
            s5 = time.monotonic()
            if self._reranker_enabled and len(results) > 1:
                try:
                    rr = self._ensure_reranker()
                    rr_scores = rr.rerank_scores(query, [c.text for c in results])
                    if len(rr_scores) == len(results):
                        ranked = sorted(
                            zip(results, rr_scores),
                            key=lambda pair: pair[1], reverse=True)
                        results = [c for c, _ in ranked[:top_k]]
                        for c, sc in ranked:
                            rerank_scores_by_id[c.chunk_id] = float(sc)
                        self._reranked = True
                    else:
                        results = results[:top_k]
                except Exception as e:  # noqa: BLE001 - rerank 故障只降级
                    self._reranker_error = f"{type(e).__name__}: {e}"
                    rerank_error = self._reranker_error
                    results = results[:top_k]
            else:
                results = results[:top_k]
            rerank_ms = (time.monotonic() - s5) * 1000
            if self._reranked:
                self._last_rerank_ms = round(rerank_ms, 3)
            if trace is not None:
                after_order = [c.chunk_id for c in results]
                rr_status = self._reranker_status()
                trace.steps.append(TraceStep(
                    name="reranker",
                    enabled=bool(self._reranked),
                    elapsed_ms=rerank_ms,
                    query=query,
                    candidates=[
                        TraceCandidate(
                            chunk_id=cid, rank=i + 1,
                            score=rerank_scores_by_id.get(cid),
                            detail={"before_rank": before_order.index(cid) + 1})
                        for i, cid in enumerate(after_order)
                    ] if self._reranked else [],
                    filtered=TraceFiltered(),
                    detail={
                        "configured": bool(self._reranker_enabled),
                        "applied": bool(self._reranked),
                        "model": self._reranker_model,
                        "backend": rr_status.get("backend"),
                        "load_result": rr_status.get("load_result"),
                        "disabled_reason": rr_status.get("disabled_reason"),
                        "before_order": before_order,
                        "after_order": after_order,
                    },
                    error=rerank_error or (None if self._reranked
                                           else rr_status.get("disabled_reason")),
                ))

            # 细粒度再保险（任何候选都不得绕过精判定）
            if access_context is not None:
                results = [c for c in results
                           if self._chunk_allowed(conn, access_context, c.document_id)]
        finally:
            conn.close()
        return RetrievalResult(
            query=query,
            chunks=results,
            route=self.ROUTE,
            timing_ms=round((time.monotonic() - t0) * 1000, 1),
            counts=self.counts(),
            status=self.retrieval_info(),
            trace=trace,
        )

    def _empty_result(self, query: str, top_k: int, t0, conn) -> RetrievalResult:
        self._active_branches = ["bm25"]
        return RetrievalResult(
            query=query, chunks=[], route=self.ROUTE,
            timing_ms=round((time.monotonic() - t0) * 1000, 1),
            counts=self.counts(), status=self.retrieval_info(),
        )

    # ---- 向量懒加载 ----

    def _ensure_vector(self, conn):
        """懒加载 embedder + VectorIndex；任何故障 → 禁用向量并返回 (None, None)。"""
        if self._vector_error is not None:
            return None, None
        if self._vector_index is not None:
            return self._vector_index, self._embedder
        index_dir = Path(self.vector_index_dir)
        if not (index_dir / "meta.json").exists() or not (index_dir / "index.faiss").exists():
            self._disable_vector("向量索引缺失")
            return None, None
        try:
            if self._embedder is None:
                self._embedder = resolve_embedder(self._model, backend=self._backend,
                                                  device=self._device)
            self._vector_index = VectorIndex.load(index_dir, self._embedder)
            return self._vector_index, self._embedder
        except Exception as e:
            self._disable_vector(f"{type(e).__name__}: {e}")
            return None, None

    def _disable_vector(self, reason: str):
        self._vector_error = reason
        self._vector_index = None
        self._active_vector_used = False

    def _ensure_reranker(self):
        if self._reranker_instance is None:
            if self._reranker_attempted and self._reranker_error:
                raise RerankerUnavailableError(self._reranker_error)
            try:
                self._reranker_instance = resolve_reranker(
                    self._reranker_model, device=self._device)
            except Exception as e:
                self._reranker_attempted = True
                self._reranker_error = f"{type(e).__name__}: {e}"
                raise RerankerUnavailableError(str(e)) from e
        return self._reranker_instance

    # ---- 授权集成辅助（在任何检索之前应用）----

    def _document_columns(self, conn: sqlite3.Connection) -> set[str]:
        return {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}

    def _document_access_clause(self, conn: sqlite3.Connection,
                                access_filter,
                                prefix: str = "") -> tuple[str | None, tuple]:
        """构建 documents 访问谓词 (clause, params)。与既有实现一致。"""
        if access_filter is None:
            return None, ()
        cols = self._document_columns(conn)
        if "classification" not in cols:
            return f"{prefix}status = ?", (access_filter.status,)
        from access.filter import build_document_access_predicate
        return build_document_access_predicate(access_filter, prefix=prefix)

    def _document_metadata(self, conn: sqlite3.Connection, document_id: str) -> dict | None:
        cols = self._document_columns(conn)
        if "classification" in cols:
            row = conn.execute(
                "SELECT classification, project_id, business_domain, status "
                "FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            return {"classification": row["classification"], "project_id": row["project_id"],
                    "business_domain": row["business_domain"], "status": row["status"]}
        row = conn.execute(
            "SELECT status FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            return None
        return {"classification": "public", "project_id": None,
                "business_domain": None, "status": row["status"]}

    def _chunk_decision(self, conn: sqlite3.Connection, access_context,
                        document_id: str) -> tuple[bool, str | None]:
        """单条候选的精判定 + 机器可读 reason（reason 仅用于聚合审计）。"""
        meta = self._document_metadata(conn, document_id)
        if meta is None:
            return False, "RESOURCE_MISSING"
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

    def _chunk_allowed(self, conn: sqlite3.Connection, access_context, document_id: str) -> bool:
        return self._chunk_decision(conn, access_context, document_id)[0]

    # ---- 候选查询 ----

    def _bm25_candidates(self, conn: sqlite3.Connection, query: str, top_n: int,
                         access_clause: str | None = None,
                         access_params: tuple = ()) -> list[RetrievedChunk]:
        match = fts_match_query(query)
        if not match or top_n <= 0:
            return []
        where_parts = ["chunks_fts MATCH ?", "c.status != 'archived'"]
        if access_clause:
            where_parts.append(access_clause)
        rows = conn.execute(
            f"""
            SELECT c.chunk_id, c.document_id, d.title, c.heading_path,
                   c.page, c.text, c.metadata_json, c.status,
                   d.metadata_json AS doc_meta_json,
                   d.classification, d.project_id, d.business_domain, d.department,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts f
            JOIN chunks c ON c.chunk_id = f.chunk_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE {" AND ".join(where_parts)}
            ORDER BY rank, c.chunk_id
            LIMIT ?
            """,
            (match, *access_params, top_n),
        ).fetchall()
        terms = query_terms_weighted(query)
        chunks = [self._make_chunk(r, score=-r["rank"], match_type="fts") for r in rows]
        chunks.sort(key=lambda c: overlap_score(c.text, terms), reverse=True)
        return chunks

    def _fetch_chunks(self, conn: sqlite3.Connection, chunk_ids: list[str]) -> dict:
        """按 chunk_id 批量取回其它分支的候选详情（返回 {chunk_id: RetrievedChunk}）。"""
        out: dict[str, RetrievedChunk] = {}
        if not chunk_ids:
            return out
        ids = list(dict.fromkeys(chunk_ids))
        ph = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT c.chunk_id, c.document_id, d.title, c.heading_path,
                   c.page, c.text, c.metadata_json, c.status,
                   d.metadata_json AS doc_meta_json,
                   d.classification, d.project_id, d.business_domain, d.department
            FROM chunks c JOIN documents d ON d.document_id = c.document_id
            WHERE c.chunk_id IN ({ph}) AND c.status != 'archived'
            """,
            ids,
        ).fetchall()
        for r in rows:
            out[r["chunk_id"]] = self._make_chunk(r, score=0.0, match_type="")
        return out

    @staticmethod
    def _make_chunk(row, score: float, match_type: str) -> RetrievedChunk:
        from knowledge.metadata import finalize_meta
        chunk_meta = json.loads(row["metadata_json"] or "{}")
        doc_meta = json.loads(row["doc_meta_json"] or "{}")
        meta = finalize_meta(
            {**doc_meta, **chunk_meta},
            department=row["department"], classification=row["classification"],
            project_id=row["project_id"], business_domain=row["business_domain"],
            status=row["status"],
        )
        return RetrievedChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            title=row["title"],
            heading_path=row["heading_path"],
            page=row["page"],
            text=row["text"],
            score=score,
            match_type=match_type,
            meta=meta,
        )
