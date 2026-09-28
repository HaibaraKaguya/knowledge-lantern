"""结构化 retrieval_trace — 仅用于测试/debug 的检索可解释性载荷.

固定顺序（对外契约，测试按此断言）：
  1. fts_bm25   —— FTS5/BM25 候选（词元 + 分数）
  2. vector     —— 向量候选（索引路径/维度/指纹 + 相似度）
  3. graph      —— GraphRAG 节点/边扩展 + 元数据（访问）过滤
  4. rrf_fusion —— 加权 RRF 融合贡献（各分支 rank/贡献分）
  5. reranker   —— rerank 前后顺序与分数

每一步都携带：实际启用状态（enabled）、耗时（elapsed_ms）、输入查询（query）、
候选 document/chunk id（candidates）、以及被权限过滤的聚合原因（filtered）。

安全契约（硬性）：
  - trace 只包含"已通过访问判定"的候选的 id 与分数；
  - 被权限过滤的候选只以聚合计数 + 机器可读 reason 码出现，
    绝不携带正文、标题、节点名或节点关系（边）；
  - 因此 trace 即使被本地测试身份读取，也不会泄漏未授权正文或图谱关系。

本模块是纯数据/序列化层，不触库、不做授权判定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: 步骤顺序契约（唯一事实源；检索层按此顺序追加）。
TRACE_ORDER: tuple[str, ...] = (
    "fts_bm25",
    "vector",
    "graph",
    "rrf_fusion",
    "reranker",
)

STEP_LABELS: dict[str, str] = {
    "fts_bm25": "FTS5/BM25 候选（词元 + 分数）",
    "vector": "向量候选（索引/相似度）",
    "graph": "GraphRAG 节点边扩展 + 元数据过滤",
    "rrf_fusion": "加权 RRF 融合贡献",
    "reranker": "reranker 前后顺序与分数",
}


def _round(value: float | None, ndigits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


@dataclass
class TraceFiltered:
    """权限过滤聚合：只记计数与 reason 码，绝不记被拒候选的 id/正文。"""

    count: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str | None, n: int = 1) -> None:
        key = reason or "UNSPECIFIED"
        self.count += n
        self.reasons[key] = self.reasons.get(key, 0) + n

    def to_dict(self) -> dict:
        return {"count": self.count, "reasons": dict(self.reasons)}


@dataclass
class TraceCandidate:
    """一条"已授权"候选（id + 分数 + 可选补充字段）。"""

    chunk_id: str
    document_id: str = ""
    rank: int | None = None
    score: float | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"chunk_id": self.chunk_id}
        if self.document_id:
            d["document_id"] = self.document_id
        if self.rank is not None:
            d["rank"] = self.rank
        if self.score is not None:
            d["score"] = _round(self.score)
        if self.detail:
            d["detail"] = dict(self.detail)
        return d


@dataclass
class TraceStep:
    """单个检索阶段的可解释记录。"""

    name: str
    enabled: bool = False
    elapsed_ms: float = 0.0
    query: str = ""
    candidates: list[TraceCandidate] = field(default_factory=list)
    filtered: TraceFiltered = field(default_factory=TraceFiltered)
    detail: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": STEP_LABELS.get(self.name, self.name),
            "enabled": self.enabled,
            "elapsed_ms": round(float(self.elapsed_ms), 3),
            "query": self.query,
            "candidates": [c.to_dict() for c in self.candidates],
            "candidate_count": len(self.candidates),
            "filtered": self.filtered.to_dict(),
            "detail": dict(self.detail),
            "error": self.error,
        }


@dataclass
class RetrievalTrace:
    """一次检索的完整 trace（步骤按 TRACE_ORDER 顺序）。"""

    query: str
    steps: list[TraceStep] = field(default_factory=list)

    def step(self, name: str) -> TraceStep | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    @property
    def order(self) -> list[str]:
        return [s.name for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "order": list(TRACE_ORDER),
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def empty_trace(query: str, *, reason: str = "空查询或无词元，未执行检索") -> RetrievalTrace:
    """空/无词元查询的占位 trace：仍按契约给出 5 个（未启用）步骤。"""
    steps = [
        TraceStep(name=name, enabled=False, query=query, error=reason)
        for name in TRACE_ORDER
    ]
    return RetrievalTrace(query=query, steps=steps)


__all__ = [
    "TRACE_ORDER",
    "STEP_LABELS",
    "TraceFiltered",
    "TraceCandidate",
    "TraceStep",
    "RetrievalTrace",
    "empty_trace",
]
