"""自动路由（DIRECT 路径）单元测试 — 路由器 JSON 分类 + 服务端 DIRECT 语义。

覆盖（对应 DIRECT 阶段 1 约束）：
- RouteClassifier：两类 JSON 输出 → INTERNAL_RETRIEVAL / DIRECT；
  非法输出、代码块包裹、异常调用一律安全回退 INTERNAL_RETRIEVAL；
- PaperQAService 默认 router=None 恒走 INTERNAL_RETRIEVAL（不触网/不触发路由器）；
- 路由器选中 DIRECT：不调用检索器、sources 为空、仍经 user_id 身份解析、
  会话归属校验、会话持久化与最小审计；检索路径生成器不被调用；
- 路由器选中 INTERNAL_RETRIEVAL 且检索未命中：不得自动改走 DIRECT；
- 会话跨用户归属拒绝发生在路由器调用之前（不消费模型调用）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from access.seed import initialize_dev_iam  # noqa: E402
from paper_qa.audit import AuditStore  # noqa: E402
from paper_qa.conversation import ConversationStore  # noqa: E402
from paper_qa.retrieval import ReadOnlyPaperRetriever  # noqa: E402
from paper_qa.router import (  # noqa: E402
    ROUTE_DIRECT,
    ROUTE_INTERNAL_RETRIEVAL,
    DirectAnswerer,
    RouteClassifier,
)
from paper_qa.schemas import PaperQAAskRequest  # noqa: E402
from paper_qa.service import AccessDeniedError, PaperQAService  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from test_paper_qa_helpers import build_knowledge_db  # noqa: E402


class FakeClient:
    """最小假 Qwen 客户端：invoke 返回带 .content 的对象或抛错。"""

    def __init__(self, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[list[dict]] = []

    def invoke(self, messages: list[dict]):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return type("Resp", (), {"content": self.content})()


class TestRouteClassifier(unittest.TestCase):
    def _classify(self, content: str | None = None, error: Exception | None = None,
                  question: str = "费用报销流程是什么") -> str:
        rc = RouteClassifier(client=FakeClient(content=content, error=error))
        return rc.classify(question)

    def test_direct_json(self):
        self.assertEqual(self._classify('{"route": "DIRECT"}'), ROUTE_DIRECT)

    def test_internal_retrieval_json(self):
        self.assertEqual(
            self._classify('{"route": "INTERNAL_RETRIEVAL"}'),
            ROUTE_INTERNAL_RETRIEVAL,
        )

    def test_code_fence_json_accepted(self):
        self.assertEqual(
            self._classify('```json\n{"route": "DIRECT"}\n```'), ROUTE_DIRECT
        )

    def test_illegal_outputs_fall_back_internal(self):
        for bad in (
            "",                      # 空内容
            "直接回答就好。",         # 非 JSON 文本
            '{"route": "MAYBE"}',   # 值非法
            '{"answer": 1}',        # 缺 route 键
            '{"route": 123}',       # route 非字符串
            "not json {{{",         # 解析失败
        ):
            self.assertEqual(self._classify(bad), ROUTE_INTERNAL_RETRIEVAL, bad)

    def test_invoke_failure_falls_back_internal(self):
        self.assertEqual(
            self._classify(error=RuntimeError("qwen down")),
            ROUTE_INTERNAL_RETRIEVAL,
        )


class TestDirectAnswerer(unittest.TestCase):
    def test_generates_without_evidence(self):
        client = FakeClient(content="直接给出建议与说明。")
        da = DirectAnswerer(client=client)
        answer = da.generate("如何做好年终总结")
        self.assertEqual(answer, "直接给出建议与说明。")
        messages = client.calls[-1]
        system, user = messages[0]["content"], messages[1]["content"]
        # DIRECT 提示不得强制证据引用规则，也不得携带任何证据块。
        self.assertNotIn("每个陈述都必须引用证据编号", system)
        self.assertNotIn("证据:", user)
        self.assertNotIn("[1]", user)

    def test_generation_failure_wraps_aiunavilable(self):
        from paper_qa.llm import AiunavilableError
        da = DirectAnswerer(client=FakeClient(error=RuntimeError("boom")))
        with self.assertRaises(AiunavilableError):
            da.generate("q")


class FakeAnswerGenerator:
    """检索路径生成器桩：DIRECT 场景下绝不应被调用。"""

    def __init__(self, answer="整合回答 [1]。", error=None):
        self.answer = answer
        self.error = error
        self.calls = 0
        self.info = {"name": "qwen-retrieval-fake", "base_url": "http://fake/v1"}

    def generate(self, question, chunks):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.answer

    def model_info(self):
        return dict(self.info)


class FakeDirectAnswerer:
    """DIRECT 路径作答器桩：记录调用，可注入 answer/error。"""

    def __init__(self, answer="直接回答内容。", error=None):
        self.answer = answer
        self.error = error
        self.calls: list[str] = []
        self.info = {"name": "qwen-direct-fake", "base_url": "http://fake/v1"}

    def generate(self, question):
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        return self.answer

    def model_info(self):
        return dict(self.info)


class FixedRouter:
    """固定路由桩：直接返回预设 route；用于服务端语义测试。"""

    def __init__(self, route: str):
        self.route = route
        self.calls = 0

    def classify(self, question):
        self.calls += 1
        return self.route


class ExplodingRouter:
    def classify(self, question):
        raise RuntimeError("router down")


class CountingRetriever(ReadOnlyPaperRetriever):
    """包装真实检索器，统计 search 调用次数（验证 DIRECT 不检索）。"""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.search_calls = 0

    def search(self, *args, **kwargs):
        self.search_calls += 1
        return super().search(*args, **kwargs)


class _DirectServiceCase(unittest.TestCase):
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

    def _service(self, router=None, direct_answerer=None):
        self.gen = FakeAnswerGenerator()
        retriever = CountingRetriever(self.kb_path)
        svc = PaperQAService(
            retriever=retriever, generator=self.gen,
            conversation=ConversationStore(self.cp_path),
            audit=AuditStore(self.audit_path), rewriter=None,
            iam_path=self.iam_path, router=router,
            direct_answerer=direct_answerer,
        )
        self._services.append(svc)
        self.retriever = retriever
        self.direct = direct_answerer
        return svc


class TestServiceAutoRouting(_DirectServiceCase):
    def test_default_without_router_is_internal_only(self):
        svc = self._service()
        out = svc.ask("费用报销怎么处理", user_id="accountant")
        self.assertEqual(out["route"], ROUTE_INTERNAL_RETRIEVAL)
        self.assertTrue(out["sources"])
        self.assertIsNone(self.direct)

    def test_router_internal_still_retrieves(self):
        router = FixedRouter(ROUTE_INTERNAL_RETRIEVAL)
        direct = FakeDirectAnswerer()
        svc = self._service(router=router, direct_answerer=direct)
        out = svc.ask("费用报销单 5000 元复核", user_id="accountant")
        self.assertEqual(out["route"], ROUTE_INTERNAL_RETRIEVAL)
        self.assertEqual(self.retriever.search_calls, 1)
        self.assertTrue(out["sources"])
        self.assertEqual(direct.calls, [])

    def test_direct_skips_retriever_and_sources_empty(self):
        router = FixedRouter(ROUTE_DIRECT)
        direct = FakeDirectAnswerer()
        svc = self._service(router=router, direct_answerer=direct)
        out = svc.ask("写一首关于财务工作的短诗", user_id="accountant")
        self.assertEqual(out["route"], ROUTE_DIRECT)
        self.assertEqual(out["sources"], [])
        self.assertIsNone(out["retrieval_query"])
        self.assertEqual(self.retriever.search_calls, 0, "DIRECT 不得调用检索器")
        self.assertEqual(self.gen.calls, 0, "DIRECT 不得走检索证据生成器")
        self.assertEqual(direct.calls, ["写一首关于财务工作的短诗"])

    def test_direct_still_owned_conversation_and_audit(self):
        router = FixedRouter(ROUTE_DIRECT)
        direct = FakeDirectAnswerer()
        svc = self._service(router=router, direct_answerer=direct)
        out = svc.ask("写一首诗", user_id="accountant")
        conv = svc.get_conversation(out["conversation_id"], "accountant")
        self.assertEqual(conv["owner_user_id"], "accountant")
        self.assertEqual(conv["turns"][-1]["route"], ROUTE_DIRECT)
        self.assertEqual(conv["turns"][-1]["sources"], [])
        events = AuditStore(self.audit_path).recent(limit=10)
        allow = [e for e in events if e["request_id"] == out["request_id"]]
        self.assertEqual(len(allow), 1)
        self.assertEqual(allow[0]["decision"], "allow")
        self.assertEqual(allow[0]["route"], ROUTE_DIRECT)
        self.assertEqual(allow[0]["user_id"], "accountant")

    def test_no_hit_never_switches_to_direct(self):
        router = FixedRouter(ROUTE_INTERNAL_RETRIEVAL)
        direct = FakeDirectAnswerer()
        svc = self._service(router=router, direct_answerer=direct)
        # 无任何知识库命中（纯 ASCII 无命中查询），也不得自动转 DIRECT。
        out = svc.ask("quantum xyzzy mars 42", user_id="accountant")
        self.assertEqual(out["route"], ROUTE_INTERNAL_RETRIEVAL)
        self.assertEqual(out["sources"], [])
        self.assertTrue(out["insufficient_evidence"])
        self.assertEqual(self.retriever.search_calls, 1)
        self.assertEqual(direct.calls, [])
        self.assertEqual(self.gen.calls, 1)

    def test_cross_user_conversation_denied_before_router(self):
        router = FixedRouter(ROUTE_DIRECT)
        direct = FakeDirectAnswerer()
        svc = self._service(router=router, direct_answerer=direct)
        conv = svc.create_conversation("accountant")
        with self.assertRaises(AccessDeniedError) as cm:
            svc.ask("q", user_id="finance_supervisor",
                    conversation_id=conv["conversation_id"])
        self.assertEqual(cm.exception.reason, "CONVERSATION_OWNERSHIP_DENIED")
        self.assertEqual(router.calls, 0, "归属拒绝应先于路由器调用")

    def test_router_illegal_or_failure_falls_back_internal(self):
        for router in (FixedRouter("BOGUS"), ExplodingRouter()):
            direct = FakeDirectAnswerer()
            svc = self._service(router=router, direct_answerer=direct)
            out = svc.ask("费用报销怎么处理", user_id="accountant")
            self.assertEqual(out["route"], ROUTE_INTERNAL_RETRIEVAL, type(router))
            self.assertEqual(self.retriever.search_calls, 1)
            self.assertEqual(direct.calls, [])

    def test_direct_unavailable_keeps_empty_sources(self):
        from paper_qa.llm import AiunavilableError
        router = FixedRouter(ROUTE_DIRECT)
        direct = FakeDirectAnswerer(error=AiunavilableError("qwen down"))
        svc = self._service(router=router, direct_answerer=direct)
        out = svc.ask("聊聊星空", user_id="accountant")
        self.assertEqual(out["route"], ROUTE_DIRECT)
        self.assertIsNone(out["answer"])
        self.assertEqual(out["model"]["status"], "unavailable")
        self.assertEqual(out["sources"], [])
        self.assertEqual(self.retriever.search_calls, 0)


class TestSchemaForbidsManualRoute(unittest.TestCase):
    def test_route_field_rejected(self):
        with self.assertRaises(ValidationError):
            PaperQAAskRequest(question="q", route="DIRECT")

    def test_normal_payload_ok(self):
        m = PaperQAAskRequest(question="费用报销流程", user_id="accountant")
        self.assertEqual(m.question, "费用报销流程")
        self.assertEqual(m.user_id, "accountant")
        self.assertIsNone(getattr(m, "route", None))


if __name__ == "__main__":
    unittest.main()
