"""加权 RRF 混合检索 — BM25/FTS、向量（BGE-M3/FAISS）与图谱扩展分支融合.

公式: fused(chunk) = Σ_branch weight / (k + rank)
  - rank 为 1 起始（分支内最优为 rank 1）
  - 某分支未命中该 chunk → 贡献 0（absent=0），但结果保留该分支 rank=None
  - 按 chunk_id 去重（同一块出现在多个分支只汇总一次）
  - 平手按 chunk_id 字典序确定性排序

默认权重: bm25=1.0, vector=1.0, graph=0.6, k=60（可由配置覆盖）。

安全/诚实约定：
  - 融合只接受"已经过访问过滤"的分支候选；
  - 只有向量分支实际可用时 fusion 才参与 vector 贡献；graph 同理；
  - 组件不可用时上层把对应分支权重置 0 并在状态里如实报告 disabled。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 分支常量（顺序即状态展示顺序）
BRANCH_BM25 = "bm25"
BRANCH_VECTOR = "vector"
BRANCH_GRAPH = "graph"
BRANCHES = (BRANCH_BM25, BRANCH_VECTOR, BRANCH_GRAPH)

DEFAULT_WEIGHTS = {BRANCH_BM25: 1.0, BRANCH_VECTOR: 1.0, BRANCH_GRAPH: 0.6}
DEFAULT_K = 60


@dataclass
class RankedFusion:
    """一条融合结果：携带可审计的分支排名/贡献。"""

    chunk_id: str
    fused_score: float
    branch_ranks: dict[str, int | None]
    branch_contributions: dict[str, float]


def _cid(item) -> str:
    if hasattr(item, "chunk_id"):
        return item.chunk_id
    return item["chunk_id"]


def rrf_fuse(branch_items: dict[str, list], weights: dict[str, float] | None = None,
             k: int = DEFAULT_K) -> list[RankedFusion]:
    """加权 RRF 融合（纯函数，可精确手算验证）。

    branch_items: {branch: [有序 items(含 chunk_id), ...]}，位置即 1 起始 rank。
    weights: 缺省使用 DEFAULT_WEIGHTS；不在 branch_items 的分支贡献 0。
    返回按 fused_score 降序、chunk_id 升序的 RankedFusion 列表。
    """
    weights = dict(DEFAULT_WEIGHTS if weights is None else weights)
    branches = sorted(b for b in BRANCHES if b in weights)
    rank_map: dict[str, dict[str, int]] = {}
    for name, items in branch_items.items():
        rank_map[name] = {_cid(it): pos for pos, it in enumerate(items, start=1)}
    contribs: dict[str, dict[str, float]] = {}
    for name, items in branch_items.items():
        w = weights.get(name, 1.0)
        if w <= 0:
            continue
        for pos, it in enumerate(items, start=1):
            cid = _cid(it)
            contribs.setdefault(cid, {})[name] = w / (k + pos)

    out: list[RankedFusion] = []
    for cid, branch_contrib in contribs.items():
        ranks = {b: rank_map.get(b, {}).get(cid) for b in branches}
        contributions = {b: branch_contrib.get(b, 0.0) for b in branches}
        out.append(RankedFusion(
            chunk_id=cid,
            fused_score=sum(contributions.values()),
            branch_ranks=ranks,
            branch_contributions=contributions,
        ))
    out.sort(key=lambda r: (-r.fused_score, r.chunk_id))
    return out


def active_branches(branch_items: dict[str, list], weights: dict[str, float]) -> list[str]:
    """实际有贡献的分支（有候选且权重 > 0）——状态展示与融合保持一致。"""
    out = []
    for b in BRANCHES:
        if weights.get(b, 0.0) > 0 and branch_items.get(b):
            out.append(b)
    return out


def contributions_present(contributions: dict[str, float]) -> int:
    """一条结果里有几个分支实际贡献了分数。"""
    return sum(1 for v in contributions.values() if v > 0)


def match_type_for(contributions: dict[str, float]) -> str:
    """给 chunk 一个可解释的 match_type 标签。

    - 多分支贡献 → hybrid_rrf
    - 单分支 → 该分支名映射（bm25 沿用既有 'fts' 标签保持兼容）
    """
    present = [b for b in BRANCHES if contributions.get(b, 0.0) > 0]
    if len(present) > 1:
        return "hybrid_rrf"
    if present == [BRANCH_BM25]:
        return "fts"
    if present == [BRANCH_VECTOR]:
        return "vector"
    if present == [BRANCH_GRAPH]:
        return "graph"
    return ""


def branch_label(branch: str) -> str:
    return {"bm25": "fts", "vector": "vector", "graph": "graph"}.get(branch, branch)
