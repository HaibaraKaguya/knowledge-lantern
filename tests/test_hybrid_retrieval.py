"""加权 RRF 融合测试 — BM25+向量融合与"无向量安全回退"链路。

覆盖需求 2：BM25+向量 RRF 融合可精确手算验证；向量分支缺失时结果等价于
纯 BM25（词元重叠排序），且状态如实报告（mode 不得虚报 hybrid_rrf）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from knowledge.hybrid_retrieval import (
    BRANCH_BM25,
    BRANCH_GRAPH,
    BRANCH_VECTOR,
    active_branches,
    match_type_for,
    rrf_fuse,
)
from knowledge.vector_index import FakeEmbedder, VectorIndex
from paper_qa.rerank import FakeReranker
from paper_qa.retrieval import ReadOnlyPaperRetriever
from test_paper_qa_helpers import build_knowledge_db


class TestRrfMath(unittest.TestCase):
    K = 60

    def test_fusion_hand_calculated(self):
        branches = {
            BRANCH_BM25: [{"chunk_id": "a"}, {"chunk_id": "b"}, {"chunk_id": "c"}],
            BRANCH_VECTOR: [{"chunk_id": "c"}, {"chunk_id": "a"}],
        }
        weights = {BRANCH_BM25: 1.0, BRANCH_VECTOR: 1.0, BRANCH_GRAPH: 0.0}
        fused = rrf_fuse(branches, weights, self.K)
        expected = {
            "a": 1.0 / (self.K + 1) + 1.0 / (self.K + 2),   # bm25@1 + vector@2
            "b": 1.0 / (self.K + 2),                          # bm25@2
            "c": 1.0 / (self.K + 3) + 1.0 / (self.K + 1),   # bm25@3 + vector@1
        }
        by_id = {f.chunk_id: f for f in fused}
        self.assertEqual(set(by_id), {"a", "b", "c"})
        for cid, want in expected.items():
            self.assertAlmostEqual(by_id[cid].fused_score, want, places=12)
        # 排序：a > c > b（a=bm25@1+vector@2 最高；确定可复现）
        self.assertEqual([f.chunk_id for f in fused], ["a", "c", "b"])
        self.assertEqual(by_id["c"].branch_ranks[BRANCH_VECTOR], 1)
        self.assertEqual(by_id["c"].branch_ranks[BRANCH_BM25], 3)

    def test_graph_absent_branch_zero_contribution(self):
        branches = {
            BRANCH_BM25: [{"chunk_id": "a"}],
            BRANCH_VECTOR: [{"chunk_id": "b"}],
            BRANCH_GRAPH: [],   # 未参与
        }
        weights = {BRANCH_BM25: 1.0, BRANCH_VECTOR: 1.0, BRANCH_GRAPH: 0.6}
        fused = rrf_fuse(branches, weights, 60)
        by_id = {f.chunk_id: f for f in fused}
        self.assertAlmostEqual(by_id["a"].branch_contributions[BRANCH_GRAPH], 0.0)
        self.assertEqual(by_id["a"].branch_ranks[BRANCH_GRAPH], None)
        self.assertEqual(active_branches(branches, weights),
                         [BRANCH_BM25, BRANCH_VECTOR])

    def test_active_branches_excludes_empty(self):
        branches = {BRANCH_BM25: [{"chunk_id": "a"}], BRANCH_VECTOR: []}
        weights = {BRANCH_BM25: 1.0, BRANCH_VECTOR: 1.0}
        self.assertEqual(active_branches(branches, weights), [BRANCH_BM25])

    def test_match_type_labels(self):
        self.assertEqual(match_type_for({"bm25": 0.01, "vector": 0.02}), "hybrid_rrf")
        self.assertEqual(match_type_for({"bm25": 0.01}), "fts")
        self.assertEqual(match_type_for({"vector": 0.01}), "vector")
        self.assertEqual(match_type_for({"graph": 0.01}), "graph")
        self.assertEqual(match_type_for({"bm25": 0.0}), "")


def _build_vector_index(db_path: str, tmpdir: str) -> str:
    """为 fixture 库建一个可重复的 FakeEmbedder 向量索引（存到 tmpdir）。"""
    from knowledge.store import KnowledgeStore
    store = KnowledgeStore(db_path)
    try:
        items = []
        for r in store.conn.execute(
            "SELECT c.chunk_id, c.text, c.heading_path, c.document_id, "
            "c.status, d.title FROM chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            "ORDER BY c.document_id, c.seq"
        ).fetchall():
            items.append({
                "chunk_id": r["chunk_id"], "text": r["text"],
                "document_id": r["document_id"], "status": r["status"],
            })
        VectorIndex(FakeEmbedder(dim=64, seed=3)).build(items).save(tmpdir)
    finally:
        store.close()
    return tmpdir


class TestRetrieverFusionAndFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        build_knowledge_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vector_off_falls_back_to_bm25(self):
        """无向量索引 → FTS 结果保留，mode 如实为 sqlite_fts5_zh。"""
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=True, graph_enabled=True)
        res = r.search("出差审批流程", top_k=5)
        self.assertGreater(len(res.chunks), 0)
        docs = {c.document_id for c in res.chunks}
        self.assertIn("D-PUB", docs)
        info = r.retrieval_info()
        self.assertEqual(info["mode"], "sqlite_fts5_zh")
        self.assertFalse(info["components"]["vector"]["active"])
        self.assertIsNotNone(info["components"]["vector"]["error"])
        self.assertFalse(info["components"]["graph"]["enabled"])

    def test_vector_on_yields_hybrid_and_truthful_status(self):
        """有向量索引（fake embedder）→ 融合生效，mode/状态如实反映 vector。"""
        vdir = str(Path(self.tmp.name) / "vindex")
        Path(vdir).mkdir()
        _build_vector_index(self.db, vdir)
        emb = FakeEmbedder(dim=64, seed=3)
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=vdir, embedder=emb,
            graph_enabled=False, reranker_enabled=False)
        res = r.search("费用报销 超支", top_k=5)
        self.assertGreater(len(res.chunks), 0)
        info = r.retrieval_info()
        self.assertEqual(info["mode"], "bm25+vector_rrf",
                         f"不应虚报/漏报分支: {info['mode']}")
        self.assertTrue(info["components"]["vector"]["active"])
        self.assertTrue(info["components"]["vector"]["index_ready"])
        self.assertEqual(info["components"]["vector"]["chunks_indexed"], 4)
        self.assertFalse(info["components"]["graph"]["enabled"])
        self.assertEqual(info["fusion"], "weighted_rrf")

    def test_match_types_marked_hybrid_only_when_two_branches(self):
        vdir = str(Path(self.tmp.name) / "vindex")
        Path(vdir).mkdir()
        _build_vector_index(self.db, vdir)
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=vdir,
            embedder=FakeEmbedder(dim=64, seed=3),
            graph_enabled=False, reranker_enabled=False)
        res = r.search("费用报销 超支", top_k=8)
        self.assertTrue(any(c.match_type == "hybrid_rrf" for c in res.chunks),
                        [c.match_type for c in res.chunks])

    def test_reranker_active_changes_order_and_status_truthful(self):
        """rerank 启用（fake 反向打分）→ 顺序变化，且状态如实反映 reranked=True。"""
        vdir = str(Path(self.tmp.name) / "vindex2")
        Path(vdir).mkdir()
        _build_vector_index(self.db, vdir)
        emb = FakeEmbedder(dim=64, seed=3)
        no_rerank = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=vdir, embedder=emb,
            graph_enabled=False, reranker_enabled=False)
        with_rerank = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=vdir, embedder=FakeEmbedder(dim=64, seed=3),
            graph_enabled=False, reranker=FakeReranker())
        q = "费用报销 超支"
        a = [c.chunk_id for c in no_rerank.search(q, top_k=3).chunks]
        b = [c.chunk_id for c in with_rerank.search(q, top_k=3).chunks]
        info = with_rerank.retrieval_info()
        self.assertTrue(info["reranked"])
        self.assertTrue(info["components"]["reranker"]["active"])
        self.assertEqual(info["mode"], "bm25+vector_rrf")
        self.assertNotEqual(a, b, "fake 反向 rerank 应改变结果顺序")

    def test_config_off_components_report_disabled(self):
        """显式关闭向量/图谱/rerank → 状态与实际一致（不虚报）。"""
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False,
                                   graph_enabled=False, reranker_enabled=False)
        r.search("出差审批流程", top_k=5)
        info = r.retrieval_info()
        self.assertEqual(info["mode"], "sqlite_fts5_zh")
        self.assertEqual(info["components"]["vector"]["configured"], False)
        self.assertEqual(info["components"]["vector"]["active"], False)
        self.assertIsNotNone(info["components"]["vector"]["error"])
        self.assertFalse(info["components"]["graph"]["enabled"])
        self.assertFalse(info["components"]["reranker"]["active"])


if __name__ == "__main__":
    unittest.main()
