"""HTTP 端点测试 — 健康/问答/会话/身份 + 知识资料工作台（服务端校验）。

对 /api/paper-qa/* 使用假服务（无网络/无真实库）；对 /api/knowledge/* 使用
真实组件（临时 DATA_DIR + IAM/KB），验证三角色在 HTTP 层的强制校验与错误语义。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from fastapi.testclient import TestClient

from access.iam import UnknownUserError
from access.seed import initialize_dev_iam

import src.server as server_mod
from src.paper_qa.service import AccessDeniedError


class FakePaperQAService:
    """鸭子类型替代 PaperQAService（仅覆盖 /api/paper-qa/* 端点）。"""

    def __init__(self):
        self.deny_ask = False
        self.no_evidence = False

    def health(self, trace_query=None):
        body = {
            "status": "ok",
            "service": "Shuchiin 部门知识问答中心",
            "paper_qa": {
                "knowledge_db": {"path": "/tmp/k.db", "mode": "read-only",
                                 "counts": {"documents": 1100, "chunks": 1100}},
                "checkpoints_db": {"path": "/tmp/c.db", "table": "paper_qa_turns",
                                   "turns": 3},
                "audit_db": {"path": "/tmp/a.db", "events": 2},
                "model": {"name": "qwen-fake", "base_url": "http://fake/v1",
                          "status": "available", "error": None},
            },
            "retrieval": {"mode": "sqlite_fts5_zh", "components": {
                "reranker": {"configured": True, "enabled": False,
                             "active": False, "load_result": "unavailable_precheck",
                             "disabled_reason": "本地无 reranker 权重",
                             "last_rerank_ms": None}}},
            "reranker": {"configured": True, "enabled": False, "active": False,
                         "model": "BAAI/bge-reranker-v2-m3",
                         "model_path": "BAAI/bge-reranker-v2-m3",
                         "resolved_path": None, "weights_present": False,
                         "dependency": {"name": "FlagEmbedding", "installed": True,
                                        "hint": None},
                         "load_attempted": False, "loaded": False,
                         "load_result": "unavailable_precheck",
                         "load_error": None,
                         "disabled_reason": "本地无 reranker 权重",
                         "last_rerank_ms": None},
            "retrieval_trace": {"enabled": False,
                                "order": ["fts_bm25", "vector", "graph",
                                          "rrf_fusion", "reranker"]},
        }
        if trace_query:
            body["retrieval_trace_sample"] = {
                "query": trace_query, "order": ["fts_bm25", "vector", "graph",
                                                "rrf_fusion", "reranker"],
                "steps": []}
        return body

    def ask(self, question, user_id=None, conversation_id=None, session_id=None,
            debug=False):
        if not question or not question.strip():
            raise ValueError("question 不能为空")
        if user_id not in (None, "employee", "accountant", "finance_supervisor"):
            raise UnknownUserError(user_id)
        if self.deny_ask:
            raise AccessDeniedError("ACTION_NOT_PERMITTED", user_id=user_id,
                                    conversation_id=conversation_id,
                                    route="INTERNAL_RETRIEVAL")
        if self.no_evidence:
            sources = []
            answer = "现有资料不足以回答该问题。"
        else:
            sources = [{"source": 1, "chunk_id": "FC-0001:1:1",
                        "document_id": "FC-0001", "title": "财务合规·月末结账",
                        "heading": "# 月度结账", "page": 1,
                        "text": "演示口径：月度结账步骤。", "score": 8.0,
                        "match_type": "fts"}]
            answer = "月度结账步骤见 [1]。"
        body = {
            "request_id": "req-test-1",
            "conversation_id": conversation_id or "conv-x",
            "user_id": user_id or "system",
            "session_id": session_id,
            "question": question.strip(),
            "retrieval_query": "月度结账 过账",
            "answer": answer,
            "sources": sources,
            "route": "INTERNAL_RETRIEVAL",
            "access": {"allowed": True, "reason": "ALLOWED"},
            "insufficient_evidence": bool(self.no_evidence),
            "timings": {"retrieval_ms": 1.0, "generation_ms": 2.0, "total_ms": 3.0},
            "model": {"name": "qwen-fake", "base_url": "http://fake/v1",
                      "status": "available", "error": None},
            "database": {"documents": 1100, "chunks": 1100, "paper_qa_turns": 4},
            "error": None,
        }
        if debug:
            body["retrieval_trace"] = {
                "query": "月度结账 过账",
                "order": ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"],
                "steps": [],
            }
        return body

    def create_conversation(self, user_id):
        if user_id not in ("employee", "accountant", "finance_supervisor"):
            raise UnknownUserError(user_id)
        return {"conversation_id": "conv-new", "owner_user_id": user_id,
                "created_at": "t"}

    def list_conversations(self, user_id):
        if user_id not in ("employee", "accountant", "finance_supervisor"):
            raise UnknownUserError(user_id)
        return [{"conversation_id": "conv-new", "owner_user_id": user_id,
                 "created_at": "t"}]

    def get_conversation(self, conversation_id, user_id):
        if user_id not in ("employee", "accountant", "finance_supervisor"):
            raise UnknownUserError(user_id)
        return {"conversation_id": conversation_id, "owner_user_id": user_id,
                "created_at": "t", "turns": []}

    def list_demo_identities(self):
        return [
            {"user_id": "employee", "user_name": "普通员工", "roles": ["employee"],
             "project_scopes": [], "business_domain_scopes": [],
             "classification_ceiling": "public"},
            {"user_id": "accountant", "user_name": "财务会计", "roles": ["accountant"],
             "project_scopes": ["finance-ledger"], "business_domain_scopes": ["finance"],
             "classification_ceiling": "internal"},
            {"user_id": "finance_supervisor", "user_name": "财务主管",
             "roles": ["finance_supervisor"],
             "project_scopes": ["finance-ledger"], "business_domain_scopes": ["finance"],
             "classification_ceiling": "restricted"},
        ]


class TestPaperQAEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original = server_mod._paper_qa_service
        cls._fake = FakePaperQAService()
        server_mod._paper_qa_service = cls._fake
        cls.client = TestClient(server_mod.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        server_mod._paper_qa_service = cls._original

    def test_health_endpoint(self):
        r = self.client.get("/api/paper-qa/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["paper_qa"]["knowledge_db"]["mode"], "read-only")
        self.assertEqual(body["paper_qa"]["knowledge_db"]["counts"]["documents"], 1100)
        self.assertEqual(body["paper_qa"]["model"]["status"], "available")

    def test_health_reports_reranker_status_and_trace_contract(self):
        body = self.client.get("/api/paper-qa/health").json()
        rr = body["reranker"]
        for key in ("configured", "enabled", "active", "model_path",
                    "weights_present", "dependency", "load_result",
                    "disabled_reason", "last_rerank_ms"):
            self.assertIn(key, rr, key)
        # 完整 trace 的读取契约（顺序 + 入口）在健康接口可读。
        self.assertEqual(
            body["retrieval_trace"]["order"],
            ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"])

    def test_ask_debug_returns_trace_default_is_concise(self):
        # 默认（不传 debug）→ 简洁响应，不含 trace。
        r = self.client.post("/api/paper-qa/ask", json={"question": "月度结账步骤？"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("retrieval_trace", r.json())
        # debug=true → 携带 retrieval_trace（服务端权限门控在 service 层测试）。
        r = self.client.post("/api/paper-qa/ask",
                             json={"question": "月度结账步骤？", "debug": True})
        self.assertEqual(r.status_code, 200)
        self.assertIn("retrieval_trace", r.json())

    def test_health_trace_query_returns_sample(self):
        r = self.client.get("/api/paper-qa/health",
                            params={"trace_query": "月度结账"})
        self.assertEqual(r.status_code, 200)
        sample = r.json()["retrieval_trace_sample"]
        self.assertEqual(sample["query"], "月度结账")
        self.assertEqual(sample["order"],
                         ["fts_bm25", "vector", "graph", "rrf_fusion", "reranker"])

    def test_ask_endpoint_success(self):
        r = self.client.post("/api/paper-qa/ask",
                             json={"question": "月度结账步骤？", "session_id": "abc"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["route"], "INTERNAL_RETRIEVAL")
        self.assertEqual(body["session_id"], "abc")
        self.assertEqual(body["sources"][0]["source"], 1)
        self.assertEqual(body["model"]["status"], "available")

    def test_ask_empty_question_returns_400(self):
        for payload in ({"question": ""}, {"question": "   "}):
            r = self.client.post("/api/paper-qa/ask", json=payload)
            self.assertEqual(r.status_code, 400, payload)
            self.assertIn("不能为空", r.json()["detail"])

    def test_ask_missing_question_returns_422(self):
        r = self.client.post("/api/paper-qa/ask", json={"session_id": "s"})
        self.assertEqual(r.status_code, 422)

    def test_ask_rejects_manual_route_selection(self):
        # 禁止用户手选路由：请求模型不提供 route 字段（extra=forbid → 422）。
        for payload in (
            {"question": "q", "route": "DIRECT"},
            {"question": "q", "route": "INTERNAL_RETRIEVAL"},
        ):
            r = self.client.post("/api/paper-qa/ask", json=payload)
            self.assertEqual(r.status_code, 422, payload)
            self.assertEqual(r.json()["detail"][0]["type"], "extra_forbidden")

    def test_route_is_internal_retrieval_only(self):
        r = self.client.post("/api/paper-qa/ask", json={"question": "q"})
        self.assertEqual(r.json()["route"], "INTERNAL_RETRIEVAL")
        self.assertNotIn("DIRECT", r.json()["route"])

    def test_ask_returns_retrieval_query_for_audit(self):
        r = self.client.post("/api/paper-qa/ask", json={"question": "月度结账步骤？"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("retrieval_query", r.json())
        self.assertEqual(r.json()["retrieval_query"], "月度结账 过账")

    def test_ask_denied_returns_403_with_reason(self):
        self._fake.deny_ask = True
        try:
            r = self.client.post("/api/paper-qa/ask",
                                 json={"question": "q", "user_id": "accountant"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["error"], "access_denied")
            self.assertEqual(r.json()["reason"], "ACTION_NOT_PERMITTED")
        finally:
            self._fake.deny_ask = False

    def test_unknown_user_returns_401(self):
        r = self.client.post("/api/paper-qa/ask",
                             json={"question": "q", "user_id": "ghost"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], "unknown_user")

    def test_insufficient_evidence_is_200_not_403(self):
        self._fake.no_evidence = True
        try:
            r = self.client.post("/api/paper-qa/ask",
                                 json={"question": "查不到", "user_id": "accountant"})
            self.assertEqual(r.status_code, 200, "证据不足≠denied access，必须 200")
            body = r.json()
            self.assertTrue(body["insufficient_evidence"])
            self.assertTrue(body["access"]["allowed"])
            self.assertEqual(body["sources"], [])
        finally:
            self._fake.no_evidence = False

    def test_conversations_endpoints(self):
        r = self.client.post("/api/paper-qa/conversations",
                             json={"user_id": "accountant"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["owner_user_id"], "accountant")

        r = self.client.post("/api/paper-qa/conversations", json={"user_id": "ghost"})
        self.assertEqual(r.status_code, 401)

        r = self.client.get("/api/paper-qa/conversations", params={"user_id": "accountant"})
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json()["conversations"], list)

        r = self.client.get("/api/paper-qa/conversations/conv-x",
                            params={"user_id": "accountant"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["conversation_id"], "conv-x")

    def test_identities_endpoint(self):
        r = self.client.get("/api/paper-qa/identities")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("contract", body)
        ids = {i["user_id"] for i in body["identities"]}
        self.assertEqual(ids, {"employee", "accountant", "finance_supervisor"})

    # ── 已物理删除的旧路由：不应再注册（请求返回 404）──

    def test_legacy_routes_removed(self):
        for method, url, payload in [
            ("post", "/api/run", {"user_input": "x"}),
            ("post", "/api/run/stream", {"user_input": "x"}),
            ("post", "/api/constitution", {"content": "x"}),
        ]:
            r = self.client.post(url, json=payload)
            self.assertIn(r.status_code, (404, 405), f"POST {url}")
        for url in ("/api/sessions", "/api/sessions/t1/history",
                    "/api/skills", "/api/constitution", "/api/pages/x.html"):
            r = self.client.get(url)
            self.assertIn(r.status_code, (404, 405), f"GET {url}")

    def test_no_websocket_route_registered(self):
        paths = {getattr(r, "path", "") for r in server_mod.app.routes}
        self.assertFalse(any("ws/" in p for p in paths), "不得注册任何 WebSocket 端点")

    def test_external_research_unavailable_over_http(self):
        r = self.client.post("/api/paper-qa/external", json={"question": "q"})
        self.assertEqual(r.status_code, 404)
        paths = {getattr(r, "path", "") for r in server_mod.app.routes}
        self.assertFalse(any("external" in p.lower() for p in paths))


class TestKnowledgeEndpoints(unittest.TestCase):
    """知识资料工作台 HTTP 语义（真实组件 + 临时 DATA_DIR）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        initialize_dev_iam(Path(self.data_dir) / "iam.db", force=True)
        self._orig_data_dir = server_mod.DATA_DIR
        server_mod.DATA_DIR = self.data_dir
        self.client = TestClient(server_mod.app)

    def tearDown(self):
        self.client.close()
        server_mod.DATA_DIR = self._orig_data_dir
        self.tmp.cleanup()

    def _documents(self, status="pending_review"):
        r = self.client.get("/api/knowledge/documents", params={"status": status})
        self.assertEqual(r.status_code, 200)
        return {d["document_id"]: d for d in r.json()["documents"]}

    def test_employee_cannot_submit(self):
        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "employee", "title": "t", "content": "正文",
            "classification": "public",
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "ACTION_NOT_PERMITTED")

    def test_supervisor_cannot_submit(self):
        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "finance_supervisor", "title": "t", "content": "正文",
            "classification": "internal",
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "ACTION_NOT_PERMITTED")

    def test_accountant_submits_and_doc_is_pending(self):
        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "费用报销补充口径", "content": "正文演示内容",
            "classification": "internal", "document_id": "SUB-001",
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["document_id"], "SUB-001")
        self.assertEqual(body["status"], "pending_review")
        pending = self._documents()
        self.assertIn("SUB-001", pending)
        # pending 资料不得出现在 active（未审核不进入检索）
        self.assertNotIn("SUB-001", self._documents(status="active"))

    def test_accountant_cannot_submit_restricted(self):
        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "t", "content": "正文",
            "classification": "restricted",
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["reason"], "CLASSIFICATION_EXCEEDS_CEILING")

    def test_invalid_classification_and_empty_content(self):
        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "t", "content": "正文",
            "classification": "top-secret",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["reason"], "INVALID_CLASSIFICATION")

        r = self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "t", "content": "   ",
            "classification": "internal",
        })
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["reason"], "EMPTY_CONTENT")

    def test_accountant_cannot_review_own_submission(self):
        """会计无审核动作：连“审自己”都到不了，直接被动作授权拒绝。"""
        self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "t", "content": "正文",
            "classification": "internal", "document_id": "SUB-SELF",
        })
        for action in ("publish", "reject"):
            r = self.client.post(f"/api/knowledge/documents/SUB-SELF/{action}",
                                 json={"user_id": "accountant", "note": "自审"})
            self.assertEqual(r.status_code, 403, action)
            self.assertEqual(r.json()["reason"], "ACTION_NOT_PERMITTED")

    def test_supervisor_publish_and_reject_flow(self):
        self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "合规检查清单", "content": "演示正文",
            "classification": "internal", "document_id": "SUB-OK",
        })
        # 非审核角色发布 → 403
        r = self.client.post("/api/knowledge/documents/SUB-OK/publish",
                             json={"user_id": "employee"})
        self.assertEqual(r.status_code, 403)
        # 主管发布
        r = self.client.post("/api/knowledge/documents/SUB-OK/publish",
                             json={"user_id": "finance_supervisor", "note": "同意"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "active")
        self.assertEqual(r.json()["reviewer_id"], "finance_supervisor")
        # 重复审核已发布资料 → 409
        r = self.client.post("/api/knowledge/documents/SUB-OK/publish",
                             json={"user_id": "finance_supervisor"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["reason"], "NOT_PENDING_REVIEW")

    def test_supervisor_reject_marks_rejected(self):
        self.client.post("/api/knowledge/ingest", json={
            "user_id": "accountant", "title": "t", "content": "正文",
            "classification": "public", "document_id": "SUB-REJ",
        })
        r = self.client.post("/api/knowledge/documents/SUB-REJ/reject",
                             json={"user_id": "finance_supervisor", "note": "口径不符"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertNotIn("SUB-REJ", self._documents(status="active"))

    def test_publish_unknown_document_404(self):
        r = self.client.post("/api/knowledge/documents/UNKNOWN/publish",
                             json={"user_id": "finance_supervisor"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["reason"], "DOCUMENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
