"""retrieval_trace 测试 — 顺序/字段、权限不泄漏、向量/图谱/reranker 开关真实、
reranker 缺失安全降级与健康报告。

trace 契约：5 步固定顺序
  fts_bm25 → vector → graph → rrf_fusion → reranker
每步带 enabled / elapsed_ms / query / candidates(document|chunk id) / filtered(reason 码)。
安全契约：trace 只含已授权候选；被权限过滤的候选只以聚合计数 + reason 码出现，
绝不携带正文/标题/节点名/节点关系。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (_ROOT / "src", _ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from access import (  # noqa: E402
    AccessContext,
    Action,
    Classification,
    build_retrieval_filter,
)
from knowledge.models import Document, GraphEdge, GraphNode  # noqa: E402
from knowledge.store import KnowledgeStore  # noqa: E402
from knowledge.vector_index import FakeEmbedder, VectorIndex  # noqa: E402
from paper_qa.rerank import FakeReranker  # noqa: E402
from paper_qa.retrieval import ReadOnlyPaperRetriever  # noqa: E402
from paper_qa.retrieval_trace import TRACE_ORDER  # noqa: E402

#: 受限文档正文/标题中的独特串（用于断言 trace 不泄漏）。
_RES_SECRET = "受限口径-绝密"
_RES_TITLE = "年度预算受限口径"


def _ctx(user_id: str, ceiling: Classification,
         project_scopes=None, domain_scopes=None) -> AccessContext:
    return AccessContext(
        user_id=user_id, user_name=user_id, roles=(user_id,),
        allowed_actions=frozenset([Action.INTERNAL_RETRIEVAL_ASK]),
        project_scopes=(None if project_scopes is None
                        else frozenset(project_scopes)),
        business_domain_scopes=(None if domain_scopes is None
                                else frozenset(domain_scopes)),
        classification_ceiling=ceiling,
    )


EMPLOYEE = _ctx("employee", Classification.PUBLIC)
ACCOUNTANT = _ctx("accountant", Classification.INTERNAL,
                  project_scopes=["finance-ledger"], domain_scopes=["finance"])
SUPERVISOR = _ctx("finance_supervisor", Classification.RESTRICTED,
                  project_scopes=["finance-ledger"], domain_scopes=["finance"])


def _node(doc_id: str, name: str, node_type: str = "module") -> GraphNode:
    return GraphNode(node_id=f"{doc_id}:{node_type}:{name}", name=name,
                     node_type=node_type, document_id=doc_id,
                     meta={"demo": True})


def _edge(doc_id: str, seq: int, src: GraphNode, dst: GraphNode,
          predicate: str = "applies_to") -> GraphEdge:
    return GraphEdge(edge_id=f"{doc_id}:e{seq:03d}", document_id=doc_id,
                     source_node_id=src.node_id, target_node_id=dst.node_id,
                     predicate=predicate, evidence_chunk_id=f"{doc_id}:1:1",
                     meta={"demo": True})


def build_trace_fixture(path: str) -> None:
    """3 篇文档（public/internal/restricted），各含 module+role 节点与一条边。"""
    store = KnowledgeStore(path)
    try:
        pub_mod, pub_role = _node("T-PUB", "出差审批"), _node("T-PUB", "员工", "role")
        res_mod, res_role = _node("T-RES", "年度预算"), _node("T-RES", "财务主管", "role")
        int_mod, int_role = _node("T-INT", "费用报销"), _node("T-INT", "会计", "role")
        store.ingest(Document(
            document_id="T-PUB", title="出差审批流程", version="1.0", status="active",
            classification="public", project_id=None, business_domain="administration",
            department="行政", content="# 出差审批\n出差审批流程与模块说明。",
            metadata={"demo": True},
            graph_nodes=[pub_mod, pub_role],
            graph_edges=[_edge("T-PUB", 1, pub_mod, pub_role)]))
        store.ingest(Document(
            document_id="T-INT", title="费用报销内部口径", version="1.0", status="active",
            classification="internal", project_id="finance-ledger",
            business_domain="finance", department="财务合规",
            content="# 费用报销\n费用报销内部口径与模块说明。",
            metadata={"demo": True},
            graph_nodes=[int_mod, int_role],
            graph_edges=[_edge("T-INT", 1, int_mod, int_role)]))
        store.ingest(Document(
            document_id="T-RES", title=_RES_TITLE, version="1.0", status="active",
            classification="restricted", project_id="finance-ledger",
            business_domain="finance", department="财务合规",
            content=f"# 年度预算\n{_RES_SECRET}：年度预算审批流程与模块说明。",
            metadata={"demo": True},
            graph_nodes=[res_mod, res_role],
            graph_edges=[_edge("T-RES", 1, res_mod, res_role)]))
    finally:
        store.close()


def build_vector_index(db_path: str, index_dir: str) -> None:
    store = KnowledgeStore(db_path)
    try:
        items = []
        for r in store.conn.execute(
            "SELECT c.chunk_id, c.text, c.document_id, c.status, d.title "
            "FROM chunks c JOIN documents d ON d.document_id = c.document_id "
            "ORDER BY c.document_id, c.seq"
        ).fetchall():
            items.append({"chunk_id": r["chunk_id"], "text": r["text"],
                          "document_id": r["document_id"], "status": r["status"]})
        VectorIndex(FakeEmbedder(dim=64, seed=7)).build(items).save(index_dir)
    finally:
        store.close()


def _candidate_doc_ids(trace_dict: dict) -> set[str]:
    out = set()
    for step in trace_dict["steps"]:
        for c in step["candidates"]:
            if c.get("document_id"):
                out.add(c["document_id"])
    return out


class TestTraceOrderAndFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        build_trace_fixture(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trace_has_five_ordered_steps_with_required_fields(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False, graph_enabled=False)
        res = r.search("出差审批流程", top_k=3, collect_trace=True)
        self.assertIsNotNone(res.trace)
        d = res.trace.to_dict()
        self.assertEqual(d["order"], list(TRACE_ORDER))
        self.assertEqual([s["name"] for s in d["steps"]], list(TRACE_ORDER))
        for step in d["steps"]:
            for key in ("name", "enabled", "elapsed_ms", "query",
                        "candidates", "filtered"):
                self.assertIn(key, step, key)
            self.assertEqual(step["query"], "出差审批流程")
            self.assertIsInstance(step["elapsed_ms"], float)
            self.assertIn("count", step["filtered"])
            self.assertIn("reasons", step["filtered"])

    def test_no_trace_when_not_requested(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False, graph_enabled=False)
        res = r.search("出差审批流程", top_k=3)
        self.assertIsNone(res.trace)

    def test_empty_query_trace_still_ordered(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False, graph_enabled=False)
        res = r.search("   ", collect_trace=True)
        d = res.trace.to_dict()
        self.assertEqual([s["name"] for s in d["steps"]], list(TRACE_ORDER))
        self.assertTrue(all(not s["enabled"] for s in d["steps"]))

    def test_fts_step_reports_tokens_and_scores(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False, graph_enabled=False)
        res = r.search("出差审批流程", top_k=3, collect_trace=True)
        fts = res.trace.step("fts_bm25").to_dict()
        self.assertTrue(fts["enabled"])
        self.assertTrue(fts["detail"]["tokens"], "FTS 步骤必须报告查询词元")
        self.assertTrue(fts["candidates"])
        self.assertIsNotNone(fts["candidates"][0]["score"])


class TestVectorStepTruthful(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        self.vdir = str(Path(self.tmp.name) / "vindex")
        build_trace_fixture(self.db)
        Path(self.vdir).mkdir()
        build_vector_index(self.db, self.vdir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_vector_step_enabled_with_index_and_scores(self):
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=self.vdir, embedder=FakeEmbedder(dim=64, seed=7),
            graph_enabled=False, reranker_enabled=False)
        res = r.search("费用报销 内部口径", top_k=3, collect_trace=True)
        v = res.trace.step("vector").to_dict()
        self.assertTrue(v["enabled"], v["error"])
        self.assertTrue(v["detail"]["index_ready"])
        self.assertEqual(v["detail"]["chunks_indexed"], 3)
        self.assertTrue(v["candidates"])
        self.assertIsNotNone(v["candidates"][0]["score"])
        self.assertIn("index_row", v["candidates"][0]["detail"])

    def test_vector_step_disabled_reports_reason(self):
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=str(Path(self.tmp.name) / "missing"),
            graph_enabled=False, reranker_enabled=False)
        res = r.search("费用报销", top_k=3, collect_trace=True)
        v = res.trace.step("vector").to_dict()
        self.assertFalse(v["enabled"])
        self.assertIsNotNone(v["error"])

    def test_vector_permission_filter_reason_and_no_leak(self):
        """employee 检索：受限向量候选被过滤，只记 reason 码，不出现其 id/正文。"""
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=self.vdir, embedder=FakeEmbedder(dim=64, seed=7),
            graph_enabled=False, reranker_enabled=False)
        res = r.search("年度预算 受限口径", top_k=3, collect_trace=True,
                       access_filter=build_retrieval_filter(EMPLOYEE),
                       access_context=EMPLOYEE)
        v = res.trace.step("vector").to_dict()
        self.assertGreaterEqual(v["filtered"]["count"], 1,
                                "受限向量候选必须被精判定过滤并计数")
        self.assertIn("CLASSIFICATION_CEILING_EXCEEDED", v["filtered"]["reasons"])
        self.assertNotIn("T-RES", _candidate_doc_ids(res.trace.to_dict()))
        blob = json.dumps(res.trace.to_dict(), ensure_ascii=False)
        self.assertNotIn(_RES_SECRET, blob)
        self.assertNotIn(_RES_TITLE, blob)


class TestGraphStepTruthful(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        build_trace_fixture(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_graph_step_enabled_with_node_edge_expansion(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False)
        res = r.search("出差审批", top_k=3, collect_trace=True,
                       access_filter=build_retrieval_filter(EMPLOYEE),
                       access_context=EMPLOYEE)
        g = res.trace.step("graph").to_dict()
        self.assertTrue(g["enabled"], g["error"])
        self.assertGreaterEqual(g["detail"]["nodes"], 1)
        self.assertGreaterEqual(g["detail"]["edges"], 1)
        self.assertTrue(g["candidates"])
        self.assertTrue(all(c.get("document_id") for c in g["candidates"]))
        # 节点/边只以计数出现，绝不输出节点名或关系谓词。
        blob = json.dumps(g, ensure_ascii=False)
        self.assertNotIn("applies_to", blob)
        self.assertNotIn("财务主管", blob)

    def test_graph_permission_filter_reason_no_leak(self):
        """access_filter=None + access_context=employee：受限节点被精判定拒绝并记 reason。"""
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False)
        res = r.search("年度预算", top_k=3, collect_trace=True,
                       access_filter=None, access_context=EMPLOYEE)
        g = res.trace.step("graph").to_dict()
        self.assertGreaterEqual(g["filtered"]["count"], 1)
        self.assertIn("CLASSIFICATION_CEILING_EXCEEDED", g["filtered"]["reasons"])
        self.assertNotIn("T-RES", _candidate_doc_ids(res.trace.to_dict()))
        # 未授权节点不得进入候选；detail 只含计数/布尔/说明，不含节点名或关系谓词。
        self.assertEqual(g["candidates"], [])
        detail_blob = json.dumps(g["detail"], ensure_ascii=False)
        self.assertNotIn("年度预算", detail_blob, "detail 不得泄漏受限节点名")
        self.assertNotIn("applies_to", detail_blob, "detail 不得泄漏边关系")
        blob = json.dumps(g, ensure_ascii=False)
        self.assertNotIn(_RES_SECRET, blob)
        self.assertNotIn("applies_to", blob)

    def test_graph_disabled_when_off(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False, graph_enabled=False)
        res = r.search("出差审批", top_k=3, collect_trace=True)
        g = res.trace.step("graph").to_dict()
        self.assertFalse(g["enabled"])
        self.assertFalse(g["detail"]["configured"])


class TestRrfAndRerankerSteps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        self.vdir = str(Path(self.tmp.name) / "vindex")
        build_trace_fixture(self.db)
        Path(self.vdir).mkdir()
        build_vector_index(self.db, self.vdir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rrf_step_reports_contributions_when_two_branches(self):
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=self.vdir, embedder=FakeEmbedder(dim=64, seed=7),
            graph_enabled=False, reranker_enabled=False)
        res = r.search("费用报销 内部口径", top_k=3, collect_trace=True,
                       access_filter=build_retrieval_filter(ACCOUNTANT),
                       access_context=ACCOUNTANT)
        rrf = res.trace.step("rrf_fusion").to_dict()
        self.assertTrue(rrf["enabled"], rrf["detail"])
        self.assertIn("bm25", rrf["detail"]["active_branches"])
        self.assertIn("vector", rrf["detail"]["active_branches"])
        top = rrf["candidates"][0]["detail"]
        self.assertIn("branch_contributions", top)
        self.assertGreaterEqual(top["contributing_branches"], 2)

    def test_reranker_step_shows_before_after_and_scores(self):
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=self.vdir, embedder=FakeEmbedder(dim=64, seed=7),
            graph_enabled=False, reranker=FakeReranker())
        res = r.search("费用报销 内部口径", top_k=3, collect_trace=True,
                       access_filter=build_retrieval_filter(ACCOUNTANT),
                       access_context=ACCOUNTANT)
        rr = res.trace.step("reranker").to_dict()
        self.assertTrue(rr["enabled"])
        self.assertTrue(rr["detail"]["applied"])
        self.assertTrue(rr["candidates"])
        self.assertIsNotNone(rr["candidates"][0]["score"])
        self.assertIn("before_rank", rr["candidates"][0]["detail"])
        self.assertNotEqual(rr["detail"]["before_order"], rr["detail"]["after_order"],
                            "fake 反向 rerank 应改变顺序")
        self.assertIsNotNone(r.retrieval_info()["components"]["reranker"]["last_rerank_ms"])

    def test_reranker_missing_safe_degradation_and_health_report(self):
        """无本地 reranker 权重 → 检索仍返回结果；trace/健康如实报告未启用原因。"""
        r = ReadOnlyPaperRetriever(
            self.db, vector_index_dir=self.vdir, embedder=FakeEmbedder(dim=64, seed=7),
            graph_enabled=False, reranker_enabled=True)
        res = r.search("费用报销 内部口径", top_k=3, collect_trace=True,
                       access_filter=build_retrieval_filter(ACCOUNTANT),
                       access_context=ACCOUNTANT)
        self.assertGreater(len(res.chunks), 0, "reranker 缺失不得阻断检索（安全降级）")
        rr = res.trace.step("reranker").to_dict()
        self.assertFalse(rr["enabled"])
        self.assertIsNotNone(rr["error"])
        self.assertEqual(rr["detail"]["before_order"], rr["detail"]["after_order"])
        status = r.retrieval_info()["components"]["reranker"]
        self.assertFalse(status["enabled"])
        self.assertFalse(status["weights_present"])
        self.assertIsNotNone(status["disabled_reason"])
        self.assertIn(status["load_result"],
                      ("unavailable_precheck", "dependency_missing", "failed"))
        self.assertIn("dependency", status)
        self.assertIn("model_path", status)
        self.assertIsNone(status["last_rerank_ms"])


class TestTraceNoLeakGeneral(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "knowledge.db")
        build_trace_fixture(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_trace_candidates_only_authorized_and_no_body_fields(self):
        r = ReadOnlyPaperRetriever(self.db, vector_enabled=False)
        res = r.search("年度预算 出差审批 费用报销", top_k=5, collect_trace=True,
                       access_filter=build_retrieval_filter(SUPERVISOR),
                       access_context=SUPERVISOR)
        trace = res.trace.to_dict()
        # supervisor 可访问受限文档；候选 id 必须是授权集合内的真实文档。
        allowed = {c.document_id for c in res.chunks}
        for cid in _candidate_doc_ids(trace):
            self.assertIn(cid, {"T-PUB", "T-INT", "T-RES"}, cid)
        # trace 候选只含 id/分数，绝不含正文/标题字段。
        for step in trace["steps"]:
            for c in step["candidates"]:
                self.assertNotIn("text", c)
                self.assertNotIn("title", c)
        self.assertTrue(allowed)  # 至少命中一篇


if __name__ == "__main__":
    unittest.main()
