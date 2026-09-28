"""问答 → 黄金证据 检索评测.

指标: hit@k（top-k 内是否有来自 gold.document_id 且（若指定）含
gold.contains 文本的块）、recall@k、MRR。

只依赖标准库，可离线复现。
"""

from __future__ import annotations
from .store import KnowledgeStore


def hit_at_k(result_ids_with_meta, gold: dict) -> bool:
    """top-k 结果中是否存在命中黄金证据的块。"""
    doc_id = gold.get("document_id")
    contains = gold.get("contains")
    for res in result_ids_with_meta:
        if res.document_id != doc_id:
            continue
        if contains and contains not in res.text:
            continue
        return True
    return False


def evaluate_retrieval(store: KnowledgeStore, questions: list,
                       top_k: int = 5, include_archived: bool = False) -> dict:
    """跑一遍评测，返回汇总指标 + 逐样本明细。"""
    samples = []
    hits = 0
    archived_ok = 0
    archived_total = 0
    for q in questions:
        question = q["question"]
        gold = q.get("gold") or {}
        results = store.retrieve(question, top_k=top_k,
                                 include_archived=include_archived)
        hit = bool(gold) and hit_at_k(results, gold)
        hits += int(hit)
        mrr = 0.0
        for rank, res in enumerate(results, start=1):
            if res.document_id == gold.get("document_id"):
                mrr = 1.0 / rank
                break
        excluded = True
        if gold.get("archived"):
            # 目标为归档文档：默认检索不应返回它
            archived_total += 1
            excluded = all(r.document_id != gold.get("document_id") for r in results)
            archived_ok += int(excluded)
        samples.append({
            "question": question,
            "gold_document_id": gold.get("document_id"),
            "gold_archived": bool(gold.get("archived")),
            "hit@%d" % top_k: hit,
            "mrr": mrr,
            "archived_excluded": excluded,
            "retrieved": [{"document_id": r.document_id, "page": r.page,
                           "version": r.version, "status": r.status,
                           "text": r.text[:120]} for r in results],
        })
    n = len(samples)
    return {
        "samples": n,
        "top_k": top_k,
        "hit@%d" % top_k: hits / n if n else 0.0,
        "recall@%d" % top_k: hits / n if n else 0.0,
        "mrr": (sum(s["mrr"] for s in samples) / n) if n else 0.0,
        "archived_exclusion_rate": archived_ok / archived_total if archived_total else None,
        "details": samples,
    }
