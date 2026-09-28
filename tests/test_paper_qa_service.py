"""PaperQAService 测试 — 真实组件集成：三角色 × 权限矩阵 + 会话 + 审计 + 健康。

权限矩阵（服务端强制，数据为 tests/test_paper_qa_helpers 的迷你语料）：
  - D-PUB（public，全员可见）        → employee / accountant / finance_supervisor 都可检索；
  - D-INT（internal+finance）        → accountant / finance_supervisor 可检索；employee 不可；
  - D-RES（restricted+finance）      → 仅 finance_supervisor 可检索；
  - D-HR（internal + 非 finance 范围）→ 无人可见（越业务域，fail-closed）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from access.seed import initialize_dev_iam  # noqa: E402
from paper_qa.audit import AuditStore  # noqa: E402
from paper_qa.conversation import ConversationStore  # noqa: E402
from paper_qa.llm import AiunavilableError  # noqa: E402
from paper_qa.retrieval import ReadOnlyPaperRetriever, RetrievedChunk, RetrievalResult  # noqa: E402
from paper_qa.service import AccessDeniedError, PaperQAService  # noqa: E402
from test_paper_qa_helpers import build_knowledge_db  # noqa: E402


class FakeGenerator:
    def __init__(self, answer="整合回答 [1]。", error=None):
        self.answer = answer
        self.error = error
        self.calls: list[tuple] = []
        self.info = {"name": "qwen-fake", "base_url": "http://fake/v1"}

    def generate(self, question, chunks):
        self.calls.append((question, chunks))
        if self.error is not None:
            raise self.error
        return self.answer

    def model_info(self):
        return dict(self.info)

    def ping(self):
        if self.error is not None:
            return {"status": "unavailable", "error": str(self.error), **self.info}
        return {"status": "available", "error": None, **self.info}


class _ServiceTestCase(unittest.TestCase):
    """共享临时目录中的真实组件（IAM / knowledge / checkpoints / audit）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.iam_path = str(Path(self.tmp.name) / "iam.db")
        self.kb_path = str(Path(self.tmp.name) / "knowledge.db")
        self.cp_path = str(Path(self.tmp.name) / "checkpoints.db")
        self.audit_path = str(Path(self.tmp.name) / "audit.db")
        self._services: list[PaperQAService] = []
        initialize_dev_iam(self.iam_path, force=True)
        build_knowledge_db(self.kb_path)

    def tearDown(self):
        for svc in self._services:
            svc.close()
        self.tmp.cleanup()

    def _service(self, generator=None, conversation=None, audit=None):
        retriever = ReadOnlyPaperRetriever(self.kb_path)
        gen = generator or FakeGenerator()
        conv = conversation or ConversationStore(self.cp_path)
        aud = audit or AuditStore(self.audit_path)
        svc = PaperQAService(
            retriever=retriever, generator=gen, conversation=conv,
            audit=aud, rewriter=None, iam_path=self.iam_path,
        )
        self._services.append(svc)
        return svc

    def _generator_doc_ids(self, svc) -> set[str]:
        if not svc._generator.calls:
            return set()
        chunks = svc._generator.calls[-1][1]
        return {c.document_id for c in chunks}


class TestPermissionMatrix(_ServiceTestCase):
    """三角色 × 迷你语料：未授权 chunk 绝不进入 sources / Qwen 载荷。"""

    def test_every_role_retrieves_public(self):
        for uid in ("employee", "accountant", "finance_supervisor"):
            svc = self._service()
            out = svc.ask("出差申请 审批 报销", user_id=uid)
            self.assertIn("D-PUB", {c["document_id"] for c in out["sources"]}, uid)
            self.assertTrue(out["access"]["allowed"])

    def test_employee_never_sees_internal_finance(self):
        svc = self._service()
        out = svc.ask("费用报销单 预算项目 5000 元复核", user_id="employee")
        doc_ids = {c["document_id"] for c in out["sources"]}
        self.assertNotIn("D-INT", doc_ids)
        self.assertNotIn("D-RES", doc_ids)
        self.assertEqual(self._generator_doc_ids(svc), doc_ids,
                         "进入 Qwen 的证据必须与 API sources 完全一致")

    def test_accountant_sees_internal_but_not_restricted(self):
        svc = self._service()
        out = svc.ask("费用报销单 预算项目 5000 元复核", user_id="accountant")
        doc_ids = {c["document_id"] for c in out["sources"]}
        self.assertIn("D-INT", doc_ids)
        self.assertNotIn("D-RES", doc_ids)

    def test_supervisor_sees_restricted(self):
        svc = self._service()
        out = svc.ask("年度预算 总经理办公会 编制口径", user_id="finance_supervisor")
        doc_ids = {c["document_id"] for c in out["sources"]}
        self.assertIn("D-RES", doc_ids)

    def test_hr_internal_invisible_even_to_supervisor(self):
        svc = self._service()
        out = svc.ask("考勤 弹性工时 迟到 备案", user_id="finance_supervisor")
        doc_ids = {c["document_id"] for c in out["sources"]}
        self.assertNotIn("D-HR", doc_ids, "finance 范围主管看不到 HR 业务域资料")

    def test_unknown_user_raises(self):
        from access.iam import UnknownUserError
        svc = self._service()
        with self.assertRaises(UnknownUserError):
            svc.ask("出差申请", user_id="ghost")

    def test_action_denied_when_role_lacks_ask(self):
        # 额外播种“只读受限员工”角色：仅 KNOWLEDGE_SUBMIT，无检索动作。
        from access.iam import IAMStore
        store = IAMStore(self.iam_path)
        store.create_role("noask", actions=("KNOWLEDGE_SUBMIT",))
        store.create_user("U-noask", user_name="无检索用户")
        store.add_user_role("U-noask", "noask")
        store.close()
        svc = self._service()
        with self.assertRaises(AccessDeniedError) as cm:
            svc.ask("出差申请", user_id="U-noask")
        self.assertEqual(cm.exception.reason, "ACTION_NOT_PERMITTED")


class TestServiceFlow(_ServiceTestCase):
    def test_ask_returns_structured_response(self):
        svc = self._service()
        out = svc.ask("费用报销怎么处理", user_id="accountant")
        self.assertEqual(out["route"], "INTERNAL_RETRIEVAL")
        self.assertIn("request_id", out)
        self.assertIn("retrieval_query", out)
        self.assertIsInstance(out["sources"], list)
        self.assertIn("timings", out)
        self.assertEqual(out["model"]["status"], "available")
        self.assertIn("conversation_id", out)

    def test_ask_auto_creates_owned_conversation(self):
        svc = self._service()
        out = svc.ask("费用报销怎么处理", user_id="accountant")
        conv_id = out["conversation_id"]
        self.assertIsNotNone(conv_id)
        conv = svc.get_conversation(conv_id, "accountant")
        self.assertEqual(conv["owner_user_id"], "accountant")

    def test_cross_user_conversation_denied(self):
        svc = self._service()
        conv = svc.create_conversation("accountant")
        with self.assertRaises(AccessDeniedError) as cm:
            svc.ask("q", user_id="finance_supervisor",
                    conversation_id=conv["conversation_id"])
        self.assertEqual(cm.exception.reason, "CONVERSATION_OWNERSHIP_DENIED")

    def test_empty_question_rejected(self):
        svc = self._service()
        with self.assertRaises(ValueError):
            svc.ask("   ", user_id="accountant")

    def test_qwen_unavailable_keeps_sources(self):
        gen = FakeGenerator(error=AiunavilableError("qwen down"))
        svc = self._service(generator=gen)
        out = svc.ask("费用报销 5000 元", user_id="accountant")
        self.assertIsNone(out["answer"])
        self.assertEqual(out["model"]["status"], "unavailable")
        self.assertIn("qwen down", out["error"])


class TestAudit(_ServiceTestCase):
    def test_allow_and_deny_recorded_without_secrets(self):
        svc = self._service()
        svc.ask("费用报销怎么处理", user_id="accountant")
        conv = svc.create_conversation("accountant")
        try:
            svc.ask("q", user_id="finance_supervisor",
                    conversation_id=conv["conversation_id"])
        except AccessDeniedError:
            pass
        events = AuditStore(self.audit_path).recent(limit=10)
        self.assertGreaterEqual(len(events), 2)
        reasons = {e["reason_code"] for e in events}
        self.assertIn("ALLOWED", reasons)
        self.assertIn("CONVERSATION_OWNERSHIP_DENIED", reasons)
        # 审计不落问题/答案原文（secrets 防护）。
        blob = repr(events)
        self.assertNotIn("费用报销", blob)
        self.assertNotIn("整合回答", blob)


class TestHealth(_ServiceTestCase):
    def test_health_structure_with_qwen_and_counts(self):
        gen = FakeGenerator()
        svc = self._service(generator=gen)
        h = svc.health()
        self.assertEqual(h["status"], "ok")
        pa = h["paper_qa"]
        self.assertEqual(pa["knowledge_db"]["mode"], "read-only")
        self.assertEqual(pa["knowledge_db"]["counts"]["documents"], 4)
        self.assertEqual(pa["checkpoints_db"]["table"], "paper_qa_turns")
        self.assertEqual(pa["audit_db"]["events"], 0)
        self.assertEqual(pa["model"]["status"], "available")
        # 扁平别名与嵌套一致
        self.assertEqual(h["model"]["status"], pa["model"]["status"])

    def test_health_qwen_unavailable(self):
        svc = self._service(generator=FakeGenerator(error=AiunavilableError("down")))
        h = svc.health()
        self.assertEqual(h["paper_qa"]["model"]["status"], "unavailable")

    def test_health_reports_real_reranker_status(self):
        """健康接口必须给出真实 reranker 报告（配置/权重/依赖/加载结果/关闭原因/耗时）。"""
        svc = self._service()
        h = svc.health()
        rr = h["reranker"]
        for key in ("configured", "enabled", "active", "model_path",
                    "weights_present", "dependency", "load_result",
                    "disabled_reason", "last_rerank_ms"):
            self.assertIn(key, rr, key)
        # 与 retrieval.components.reranker 同源一致（同一对象）。
        self.assertEqual(rr, h["retrieval"]["components"]["reranker"])
        self.assertFalse(rr["enabled"])
        self.assertIsNotNone(rr["disabled_reason"])
        # 本环境无本地 reranker 权重 → 依赖虽装但权重缺失（或依赖缺失），必须如实。
        self.assertIn(rr["load_result"],
                      ("unavailable_precheck", "dependency_missing", "failed"))
        self.assertEqual(
            h["retrieval_trace"]["order"],
            ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"])


class TestDebugTraceGating(_ServiceTestCase):
    """完整 retrieval_trace 的读取门控：默认简洁；仅本地测试身份/debug 配置可读。"""

    def test_default_response_is_concise(self):
        svc = self._service()
        out = svc.ask("费用报销怎么处理", user_id="accountant")
        self.assertNotIn("retrieval_trace", out)

    def test_non_local_identity_without_debug_config_gets_no_trace(self):
        svc = self._service()
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "0"}):
            out = svc.ask("费用报销怎么处理", user_id="accountant", debug=True)
        self.assertNotIn("retrieval_trace", out)

    def test_local_test_identity_can_read_trace(self):
        svc = self._service()
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "0"}):
            out = svc.ask("费用报销怎么处理", debug=True)  # user_id=None → 本地测试身份
        self.assertIn("retrieval_trace", out)
        trace = out["retrieval_trace"]
        self.assertEqual(
            trace["order"],
            ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"])
        self.assertEqual([s["name"] for s in trace["steps"]], trace["order"])
        for step in trace["steps"]:
            for key in ("name", "enabled", "elapsed_ms", "query",
                        "candidates", "filtered"):
                self.assertIn(key, step, key)

    def test_debug_config_enables_trace_for_named_identity(self):
        svc = self._service()
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "1"}):
            out = svc.ask("费用报销怎么处理", user_id="accountant", debug=True)
        self.assertIn("retrieval_trace", out)
        self.assertTrue(out["retrieval_trace"]["query"])

    def test_trace_never_leaks_unauthorized_documents(self):
        """employee 请求 trace：未授权文档 id 不得出现在任何 trace 候选里。"""
        svc = self._service()
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "1"}):
            out = svc.ask("费用报销单 预算项目 5000 元复核",
                          user_id="employee", debug=True)
        trace = out["retrieval_trace"]
        doc_ids = set()
        for step in trace["steps"]:
            for cand in step["candidates"]:
                if cand.get("document_id"):
                    doc_ids.add(cand["document_id"])
        self.assertNotIn("D-INT", doc_ids)
        self.assertNotIn("D-RES", doc_ids)
        self.assertNotIn("D-HR", doc_ids)

    def test_health_trace_query_gated_by_debug_config(self):
        svc = self._service()
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "0"}):
            h = svc.health(trace_query="费用报销")
        self.assertIn("error", h["retrieval_trace_sample"])
        with mock.patch.dict(os.environ, {"SRC_RETRIEVAL_TRACE": "1"}):
            h = svc.health(trace_query="费用报销")
        sample = h["retrieval_trace_sample"]
        self.assertEqual(sample["query"], "费用报销")
        self.assertEqual(sample["order"],
                         ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"])
        self.assertEqual([s["name"] for s in sample["steps"]], sample["order"])


if __name__ == "__main__":
    unittest.main()
