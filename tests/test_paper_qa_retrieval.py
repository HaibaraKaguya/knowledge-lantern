"""ReadOnlyPaperRetriever 测试 — 只读 SQLite FTS5 检索 / 授权过滤 / 只读保证。"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_qa.retrieval import ReadOnlyPaperRetriever
from test_paper_qa_helpers import build_knowledge_db, insert_archived_doc


def _tmp_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    build_knowledge_db(tmp.name)
    return tmp.name


class TestRetrieval(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        self.r = ReadOnlyPaperRetriever(self.db)

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_search_returns_relevant_docs(self):
        res = self.r.search("出差审批流程", top_k=5)
        docs = {c.document_id for c in res.chunks}
        self.assertIn("D-PUB", docs)

    def test_search_finance_internal(self):
        res = self.r.search("费用报销 超支", top_k=5)
        docs = {c.document_id for c in res.chunks}
        self.assertIn("D-INT", docs)

    def test_results_have_provenance(self):
        res = self.r.search("预算 编制", top_k=5)
        self.assertGreater(len(res.chunks), 0)
        c = res.chunks[0]
        self.assertTrue(c.chunk_id.startswith(c.document_id))
        self.assertTrue(c.title)
        self.assertTrue(c.heading_path)
        self.assertGreater(c.page, 0)
        self.assertEqual(c.match_type, "fts")

    def test_deterministic_ordering(self):
        q = "费用报销"
        first = [c.chunk_id for c in self.r.search(q, top_k=10).chunks]
        second = [c.chunk_id for c in self.r.search(q, top_k=10).chunks]
        self.assertEqual(first, second)
        self.assertEqual(len(first), len(set(first)), "chunk_id 不应重复")

    def test_empty_query_returns_empty(self):
        res = self.r.search("   ")
        self.assertEqual(res.chunks, [])

    def test_no_terms_query_returns_empty(self):
        res = self.r.search("!!! ??? --")
        self.assertEqual(res.chunks, [])

    def test_sql_injection_is_safe(self):
        malicious = '"); DROP TABLE chunks; --'
        res = self.r.search(malicious, top_k=5)
        self.assertIsInstance(res.chunks, list)
        conn = sqlite3.connect(self.db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        self.assertIn("chunks", tables, "chunks 表必须仍然存在")

    def test_quoted_terms_safe(self):
        res = self.r.search('报销 "审批"', top_k=5)
        self.assertIsInstance(res.chunks, list)

    def test_route_field(self):
        self.assertEqual(self.r.search("报销").route, "INTERNAL_RETRIEVAL")

    def test_retrieval_info(self):
        info = self.r.retrieval_info()
        self.assertEqual(info["corpus"], "department_demo_knowledge")


class TestReadOnly(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        self.r = ReadOnlyPaperRetriever(self.db)

    def tearDown(self):
        Path(self.db).unlink(missing_ok=True)

    def test_connection_is_readonly(self):
        conn = self.r._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO documents (document_id,title,version) VALUES ('x','y','z')"
                )
        finally:
            conn.close()

    def test_counts(self):
        counts = self.r.counts()
        self.assertEqual(counts["documents"], 4)
        self.assertGreater(counts["chunks"], 0)


class TestArchived(unittest.TestCase):
    def test_archived_chunks_excluded(self):
        db = _tmp_db()
        insert_archived_doc(db)
        r = ReadOnlyPaperRetriever(db)
        try:
            res = r.search("纸面审批 旧规则", top_k=10)
            self.assertTrue(all(c.document_id != "D-ARCH" for c in res.chunks))
        finally:
            Path(db).unlink(missing_ok=True)


class TestRealKnowledgeDB(unittest.TestCase):
    """对真实 data/knowledge.db（五部门演示语料）的只读冒烟测试（文件存在时执行）。"""

    @unittest.skipUnless(
        Path(__file__).resolve().parents[1].joinpath("data/knowledge.db").exists(),
        "真实 knowledge.db 不存在，跳过",
    )
    def test_real_db_retrieval_and_counts(self):
        db = Path(__file__).resolve().parents[1] / "data/knowledge.db"
        r = ReadOnlyPaperRetriever(db)
        counts = r.counts()
        # 演示基线：1100 篇 demo 文档（另有资料工作台条目可能共存，按 demo 源计数）
        conn = r._connect()
        try:
            demo_docs = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source='demo-knowledge-builder'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(demo_docs, 1100)
        self.assertGreaterEqual(counts["documents"], 1100)
        self.assertGreaterEqual(counts["chunks"], 1100)
        res = r.search("月度结账 过账", top_k=5)
        self.assertGreater(len(res.chunks), 0)
        again = r.search("月度结账 过账", top_k=5)
        self.assertEqual([c.chunk_id for c in res.chunks],
                         [c.chunk_id for c in again.chunks])
        # 无向量/图谱（组件缺失或空库）时 mode 必须如实，不得虚报 hybrid_rrf/graph
        info = r.retrieval_info()
        if not info["components"]["vector"]["active"]:
            self.assertNotIn("hybrid", info["mode"])
            self.assertNotIn("vector", info["mode"])
        if not info["components"]["graph"]["enabled"]:
            self.assertNotIn("graph", info["mode"])


if __name__ == "__main__":
    unittest.main()
