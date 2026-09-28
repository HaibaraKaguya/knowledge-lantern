"""知识问答编排服务 — question → 自动路由 → SQLite 检索/直接作答 → 模型服务.

链路（部门知识问答中心的闭环）：
  问题 → 服务端身份解析（iam.db）→ 动作授权 → 会话归属
      → 自动路由（仅服务端模型路由器，用户不可手选）：
        · INTERNAL_RETRIEVAL → 查询清洗 → 只读检索（FTS5 + 授权过滤，逐条精判定）
          → 模型服务依据证据生成 → 会话轮次持久化 → 审计事实落库
        · DIRECT → 模型服务直接作答（不检索知识库，sources 为空）
          → 会话轮次持久化 → 审计事实落库
      → 结构化响应（route / 来源 / 检索词 / 计时 / 模型状态）

安全契约：
- user_id → 服务端 IAM 构建不可变 AccessContext（拒绝客户端伪造 role/scope）；
- 检索前用 build_retrieval_filter 生成参数化过滤，检索中对每条候选逐条 decide()，
  未授权 chunk 永不进入模型服务或 API source 载荷；
- DIRECT 只在路由器于检索前选中时生效；检索未命中证据绝不自动转 DIRECT；
- 会话由服务端拥有（conversation_id + owner_user_id），读写校验归属；
- 每次请求写入独立审计库（append-only、事务安全、不落 secrets）。
"""

from __future__ import annotations
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from access import (
    AccessContext,
    Action,
    Classification,
    IAMStore,
    build_retrieval_filter,
)

from .audit import AuditStore, new_request_id
from .conversation import (
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationStore,
)
from .llm import AiunavilableError, PaperAnswerGenerator
from .retrieval import ReadOnlyPaperRetriever, trace_enabled
from .retrieval_trace import TRACE_ORDER
from .rewrite import QueryRewriter
from .router import ROUTE_DIRECT, ROUTE_INTERNAL_RETRIEVAL

logger = logging.getLogger("knowledge_lantern.qa")

_ROUTE = ROUTE_INTERNAL_RETRIEVAL

REASON_ALLOWED = "ALLOWED"
REASON_ACTION_NOT_PERMITTED = "ACTION_NOT_PERMITTED"
REASON_CONVERSATION_OWNERSHIP_DENIED = "CONVERSATION_OWNERSHIP_DENIED"


class AccessDeniedError(Exception):
    """授权层拒绝（HTTP 层映射为 403）。携带机器可读 reason。"""

    def __init__(self, reason: str, *, user_id: str | None = None,
                 conversation_id: str | None = None,
                 route: str | None = None, request_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.route = route
        self.request_id = request_id


def system_access_context() -> AccessContext:
    """全权限系统上下文（向后兼容匿名调用；HTTP 层总是传入真实 user_id）。

    服务端未传 user_id 的旧式调用（如既有测试）继续可用，但只应在
    无认证的本地/测试路径出现；生产 HTTP 层必须显式选择 demo 身份。
    """
    return AccessContext(
        user_id="system",
        user_name="System",
        roles=("system",),
        allowed_actions=frozenset(Action),
        project_scopes=None,
        business_domain_scopes=None,
        classification_ceiling=Classification.RESTRICTED,
    )


class PaperQAService:
    ROUTE = _ROUTE

    def __init__(self, *, knowledge_db_path: str | Path | None = None,
                 checkpoint_db_path: str | Path | None = None,
                 audit_db_path: str | Path | None = None,
                 iam_path: str | Path | None = None,
                 retriever: Any | None = None,
                 generator: Any | None = None,
                 conversation: Any | None = None,
                 rewriter: Any | None = None,
                 iam: Any | None = None,
                 audit: Any | None = None,
                 router: Any | None = None,
                 direct_answerer: Any | None = None,
                 top_k: int = 5):
        if retriever is None:
            retriever = ReadOnlyPaperRetriever(
                knowledge_db_path or self._default_knowledge_db())
        if generator is None:
            generator = PaperAnswerGenerator()
        if conversation is None:
            conversation = ConversationStore(
                checkpoint_db_path or self._default_checkpoint_db()
            )
        if rewriter is None:
            rewriter = QueryRewriter()
        self._retriever = retriever
        self._generator = generator
        self._conversation = conversation
        self._rewriter = rewriter
        self._iam = iam
        self._iam_path = iam_path
        self._audit = audit
        self._audit_db_path = audit_db_path
        # 自动路由装配：router=None → 恒走 INTERNAL_RETRIEVAL（现有测试不触网）；
        # 仅生产装配（server.py）显式注入 RouteClassifier + DirectAnswerer。
        self._router = router
        self._direct_answerer = direct_answerer
        self.top_k = top_k

    # ---- 默认路径 ----

    @staticmethod
    def _default_knowledge_db() -> str:
        from src.utils.files import DATA_DIR
        return str(Path(DATA_DIR) / "knowledge.db")

    @staticmethod
    def _default_checkpoint_db() -> str:
        from src.utils.files import DATA_DIR
        return str(Path(DATA_DIR) / "checkpoints.db")

    @staticmethod
    def _default_audit_db() -> str:
        from src.utils.files import DATA_DIR
        return str(Path(DATA_DIR) / "audit.db")

    @staticmethod
    def _default_iam_db() -> str:
        from src.utils.files import DATA_DIR
        return str(Path(DATA_DIR) / "iam.db")

    # ---- 服务端身份 / 审计 ----

    def _audit_store(self) -> AuditStore:
        if self._audit is None:
            if self._audit_db_path is None:
                self._audit_db_path = self._default_audit_db()
            self._audit = AuditStore(self._audit_db_path)
        return self._audit

    def _resolve_context(self, user_id: str | None) -> AccessContext:
        """服务端从 user_id 构建 AccessContext；None → 系统全权限（测试/旧调用）。

        每次解析使用短生命周期 IAMStore 连接（with 自动 close）：
        FastAPI 同步端点跑在线程池，长连接跨线程复用会触发
        sqlite3.ProgrammingError（连接只能在同一线程使用）。
        """
        if user_id is None:
            return system_access_context()
        if self._iam is not None:  # 注入实例：调用方管理生命周期（单线程测试友好）
            return AccessContext.from_user(self._iam, user_id)
        db = self._iam_path or self._default_iam_db()
        with IAMStore(db) as store:
            return AccessContext.from_user(store, user_id)

    def _audit_allow(self, request_id: str, user_id: str,
                     conversation_id: str | None, route: str,
                     allowed_source_ids: list[str]) -> None:
        try:
            self._audit_store().record(
                request_id=request_id, user_id=user_id, conversation_id=conversation_id,
                route=route, decision="allow", reason_code=REASON_ALLOWED,
                allowed_source_ids=allowed_source_ids)
        except Exception as e:  # 审计写失败不阻断主流程，但必须记录
            logger.warning("audit record failed (allow): %s", e)

    def _audit_deny(self, request_id: str, user_id: str,
                    conversation_id: str | None, route: str, reason: str) -> None:
        try:
            self._audit_store().record(
                request_id=request_id, user_id=user_id, conversation_id=conversation_id,
                route=route, decision="deny", reason_code=reason, allowed_source_ids=[])
        except Exception as e:
            logger.warning("audit record failed (deny): %s", e)

    def _resolve_conversation(self, ctx: AccessContext, user_id: str | None,
                              conversation_id: str | None) -> str | None:
        """解析/创建由 user_id 拥有的会话，返回 conversation_id。

        - user_id None（遗留匿名）：返回 None（调用方用 session_id）。
        - conversation_id 给定：校验归属；跨用户访问 → AccessDeniedError。
        - conversation_id None：为该用户新建会话。
        """
        if user_id is None:
            return None
        if conversation_id is not None:
            try:
                self._conversation.get_conversation(conversation_id, user_id)
            except ConversationOwnershipError:
                self._audit_deny(new_request_id(), ctx.user_id, conversation_id,
                                 self.ROUTE, REASON_CONVERSATION_OWNERSHIP_DENIED)
                raise AccessDeniedError(
                    REASON_CONVERSATION_OWNERSHIP_DENIED, user_id=ctx.user_id,
                    conversation_id=conversation_id, route=self.ROUTE)
            return conversation_id
        conv = self._conversation.create_conversation(user_id)
        return conv["conversation_id"]

    # ---- 自动路由问答入口 ----

    def ask(self, question: str, *, user_id: str | None = None,
            conversation_id: str | None = None, session_id: str | None = None,
            debug: bool = False) -> dict:
        """一次问答：授权 → 会话归属 → 自动路由 → 检索/直接生成 → 持久化 → 审计。

        自动路由仅由服务端模型路由器选择（禁止用户手选）：
        - INTERNAL_RETRIEVAL：查询清洗 → 检索（过滤） → 依据证据生成，
          sources 恒为授权可见内容；
        - DIRECT：不检索知识库，模型服务直接作答，sources 恒为空；
          仍复用 user_id 身份解析、会话归属校验、会话持久化与最小审计。
        检索未命中证据不会自动改走 DIRECT（路由决策只发生在检索之前）。

        debug=True 时请求结构化 retrieval_trace，但**仅当**服务端显式开启 debug
        配置（SRC_RETRIEVAL_TRACE/retrieval.trace.enabled）或调用为本地测试身份
        （user_id=None）时才真正返回；普通用户即使传 true 也只得到简洁响应。

        返回结构化响应：route / access / conversation_id / sources（限授权内容）/
        insufficient_evidence（区别于 denied access）/ request_id。
        """
        q = self._validate_question(question)
        ctx = self._resolve_context(user_id)
        request_id = new_request_id()
        expose_trace = self._trace_permitted(user_id, debug)

        if Action.INTERNAL_RETRIEVAL_ASK not in ctx.allowed_actions:
            self._audit_deny(request_id, ctx.user_id, conversation_id,
                             self.ROUTE, REASON_ACTION_NOT_PERMITTED)
            raise AccessDeniedError(
                REASON_ACTION_NOT_PERMITTED, user_id=ctx.user_id,
                conversation_id=conversation_id, route=self.ROUTE, request_id=request_id)

        conv_id = self._resolve_conversation(ctx, user_id, conversation_id)
        route = self._select_route(q)
        logger.info(
            "ask start: request_id=%s user_id=%s route=%s trace=%s question=%r",
            request_id, ctx.user_id, route, expose_trace, q,
        )

        if route == ROUTE_DIRECT:
            return self._ask_direct(
                question=q, ctx=ctx, user_id=user_id, conversation_id=conv_id,
                session_id=session_id, request_id=request_id,
                expose_trace=expose_trace,
            )
        return self._ask_internal(
            question=q, ctx=ctx, user_id=user_id, conversation_id=conv_id,
            session_id=session_id, request_id=request_id,
            expose_trace=expose_trace,
        )

    @staticmethod
    def _trace_permitted(user_id: str | None, debug: bool) -> bool:
        """是否允许返回完整 trace：需显式请求 + (本地测试身份 或 明确 debug 配置)。"""
        if not debug:
            return False
        return user_id is None or trace_enabled()

    def _select_route(self, question: str) -> str:
        """模型自动路由；router 未装配 / 非法输出 / 调用失败一律 INTERNAL_RETRIEVAL。

        只有同时装配了路由器和 DIRECT 作答器才可能走 DIRECT；否则即便模型
        输出 DIRECT 也回退 INTERNAL_RETRIEVAL（fail-closed，绝不误入 DIRECT）。
        """
        if self._router is None or self._direct_answerer is None:
            return ROUTE_INTERNAL_RETRIEVAL
        try:
            route = self._router.classify(question)
        except Exception as e:
            logger.warning("router failed, fallback %s: %s",
                           ROUTE_INTERNAL_RETRIEVAL, e)
            return ROUTE_INTERNAL_RETRIEVAL
        if route != ROUTE_DIRECT:
            logger.warning("router returned %r, fallback %s",
                           route, ROUTE_INTERNAL_RETRIEVAL)
            return ROUTE_INTERNAL_RETRIEVAL
        return ROUTE_DIRECT

    def _record_turn(self, question: str, answer: str, sources_json: str,
                     route: str, model_status: str, user_id: str | None,
                     conversation_id: str | None,
                     session_id: str | None) -> str | None:
        """持久化问答轮次（服务端会话/遗留匿名）；返回应回传的 session_id。

        - user_id 给定：写入由 conversation_id 归属的会话轮次；
        - user_id None（遗留匿名）：写入无主轮次，缺省生成新 session_id。
        """
        if user_id is not None:
            self._conversation.add_turn(
                question=question, answer=answer, sources_json=sources_json,
                conversation_id=conversation_id, owner_user_id=user_id,
                model_status=model_status, route=route,
            )
            return session_id
        sid = session_id or self._new_session_id()
        self._conversation.add_turn(
            question=question, answer=answer, sources_json=sources_json,
            session_id=sid, model_status=model_status, route=route,
        )
        return sid

    # ---- 内部检索问答 ----

    def _ask_internal(self, question: str, *, ctx: AccessContext,
                      user_id: str | None, conversation_id: str | None,
                      session_id: str | None, request_id: str,
                      expose_trace: bool = False) -> dict:
        """INTERNAL_RETRIEVAL：授权检索（过滤） → 依据证据生成 → 持久化 → 审计。"""
        route = ROUTE_INTERNAL_RETRIEVAL
        retrieval_query = self._rewriter.rewrite(question)
        filt = build_retrieval_filter(ctx)

        t0 = time.monotonic()
        retrieval = self._retriever.search(
            retrieval_query, top_k=self.top_k,
            access_filter=filt, access_context=ctx,
            collect_trace=expose_trace)
        t1 = time.monotonic()

        try:
            answer = self._generator.generate(question, retrieval.chunks)
            model_status = "available"
            model_error = None
        except AiunavilableError as e:
            answer = None
            model_status = "unavailable"
            model_error = str(e)

        t2 = time.monotonic()

        sources = [c.to_dict(source_number=i + 1)
                   for i, c in enumerate(retrieval.chunks)]
        allowed_source_ids = [c.document_id for c in retrieval.chunks]

        sources_json = json.dumps(sources, ensure_ascii=False)
        session_id = self._record_turn(
            question, answer or "", sources_json, route, model_status,
            user_id, conversation_id, session_id)

        self._audit_allow(request_id, ctx.user_id, conversation_id, route,
                          allowed_source_ids)

        info = self._generator.model_info()
        logger.info(
            "ask done: request_id=%s user_id=%s route=%s conversation_id=%s "
            "retrieval_query=%r sources=%d model=%s status=%s",
            request_id, ctx.user_id, route, conversation_id, retrieval_query,
            len(sources), info.get("name"), model_status,
        )
        response = {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "user_id": ctx.user_id,
            "session_id": session_id,
            "question": question,
            "retrieval_query": retrieval_query,
            "answer": answer,
            "sources": sources,
            "route": route,
            "access": {"allowed": True, "reason": REASON_ALLOWED},
            "insufficient_evidence": not sources,
            "timings": {
                "retrieval_ms": round((t1 - t0) * 1000, 1),
                "generation_ms": round((t2 - t1) * 1000, 1),
                "total_ms": round((t2 - t0) * 1000, 1),
            },
            "model": {
                "name": info["name"],
                "base_url": info["base_url"],
                "status": model_status,
                "error": model_error,
            },
            "database": {
                **retrieval.counts,
                "paper_qa_turns": self._turn_count(user_id, conversation_id),
            },
            "retrieval": self._retriever.retrieval_info(),
            "error": model_error,
        }
        if expose_trace:
            response["retrieval_trace"] = (
                retrieval.trace.to_dict() if retrieval.trace is not None else None)
        return response

    def _ask_direct(self, question: str, *, ctx: AccessContext,
                    user_id: str | None, conversation_id: str | None,
                    session_id: str | None, request_id: str,
                    expose_trace: bool = False) -> dict:
        """DIRECT：不调用检索器、sources 为空；仍持久化会话并写审计。"""
        route = ROUTE_DIRECT
        t0 = time.monotonic()
        try:
            answer = self._direct_answerer.generate(question)
            model_status = "available"
            model_error = None
        except AiunavilableError as e:
            answer = None
            model_status = "unavailable"
            model_error = str(e)
        t1 = time.monotonic()

        sources_json = "[]"
        session_id = self._record_turn(
            question, answer or "", sources_json, route, model_status,
            user_id, conversation_id, session_id)
        self._audit_allow(request_id, ctx.user_id, conversation_id, route, [])

        info = self._direct_answerer.model_info()
        logger.info(
            "ask done (direct): request_id=%s user_id=%s route=%s conversation_id=%s "
            "sources=0 model=%s status=%s",
            request_id, ctx.user_id, route, conversation_id,
            info.get("name"), model_status,
        )
        response = {
            "request_id": request_id,
            "conversation_id": conversation_id,
            "user_id": ctx.user_id,
            "session_id": session_id,
            "question": question,
            "retrieval_query": None,
            "answer": answer,
            "sources": [],
            "route": route,
            "access": {"allowed": True, "reason": REASON_ALLOWED},
            "insufficient_evidence": False,
            "timings": {
                "retrieval_ms": 0.0,
                "generation_ms": round((t1 - t0) * 1000, 1),
                "total_ms": round((t1 - t0) * 1000, 1),
            },
            "model": {
                "name": info["name"],
                "base_url": info["base_url"],
                "status": model_status,
                "error": model_error,
            },
            "database": {
                "mode": "direct",
                "paper_qa_turns": self._turn_count(user_id, conversation_id),
            },
            "retrieval": {"mode": "none", "detail": "DIRECT 路径未检索知识库"},
            "error": model_error,
        }
        if expose_trace:
            # DIRECT 未检索知识库 → 无 trace（明确置空，避免歧义）。
            response["retrieval_trace"] = None
        return response

    # ---- 会话端点辅助（服务端拥有会话）----

    def create_conversation(self, user_id: str) -> dict:
        """创建由 user_id 拥有的会话（校验用户身份；返回 conversation_id）。"""
        self._resolve_context(user_id)  # 用户不存在/未启用 → UnknownUserError
        return self._conversation.create_conversation(user_id)

    def get_conversation(self, conversation_id: str, user_id: str) -> dict:
        """读取会话 + 轮次（归属校验；跨用户 → ConversationOwnershipError）。"""
        self._resolve_context(user_id)
        conv = self._conversation.get_conversation(conversation_id, user_id)
        turns = self._conversation.get_conversation_turns(conversation_id, user_id)
        return {**conv, "turns": turns}

    def list_conversations(self, user_id: str) -> list[dict]:
        self._resolve_context(user_id)
        return self._conversation.list_conversations(user_id)

    def list_demo_identities(self) -> list[dict]:
        """演示身份发现（本地开发/测试选择契约；不含任何认证材料）。"""
        from access import DEMO_IDENTITIES
        return [
            {
                "user_id": d.user_id,
                "user_name": d.user_name,
                "roles": list(d.roles),
                "project_scopes": list(d.project_scopes),
                "business_domain_scopes": list(d.business_domain_scopes),
                "classification_ceiling": d.classification_ceiling,
            }
            for d in DEMO_IDENTITIES
        ]

    def _turn_count(self, user_id: str | None, conv_id: str | None) -> int:
        if user_id is not None and conv_id is not None:
            return self._conversation.count_turns(conversation_id=conv_id, owner_user_id=user_id)
        return self._conversation.count_turns()

    def close(self) -> None:
        """释放懒加载的服务端连接（IAM）。测试与进程退出时调用，避免泄漏。"""
        if self._iam is not None:
            try:
                self._iam.close()
            except Exception:
                pass
            self._iam = None

    # ---- 健康检查 / 审计状态 ----

    def health(self, *, trace_query: str | None = None) -> dict:
        """健康检查：知识库只读计数 + 会话/审计状态 + 模型服务探测 + reranker 状态。

        同时返回 `paper_qa` 嵌套契约（static 前端读取的正式形状）与一组
        扁平别名（向后兼容旧客户端）；计数与状态来自同一对象，内容一致。

        trace_query 给定且**显式开启 debug 配置**（SRC_RETRIEVAL_TRACE/
        retrieval.trace.enabled）时，健康接口额外返回一次完整 retrieval_trace
        （本地测试/运维排查通道）；未开启 debug 时只返回提示，不执行检索，
        避免在无身份的 health 上默认泄漏受限内容。
        """
        k_counts = self._retriever.counts()
        turns = self._conversation.count_turns()
        model = self._generator.ping()
        audit = self._audit_status()
        knowledge_db = {
            "path": self._retriever.db_path,
            "mode": "read-only",
            "counts": k_counts,
        }
        checkpoints_db = {
            "path": self._conversation.db_path,
            "table": "paper_qa_turns",
            "turns": turns,
        }
        audit_db = audit
        paper_qa = {
            "knowledge_db": knowledge_db,
            "checkpoints_db": checkpoints_db,
            "audit_db": audit_db,
            "model": model,
        }
        debug_on = trace_enabled()
        retrieval = self._retriever.retrieval_info()
        components = retrieval.get("components", {}) if isinstance(retrieval, dict) else {}
        reranker = components.get("reranker", {})
        result = {
            "status": "ok",
            "service": "Knowledge Lantern 部门知识问答中心",
            "paper_qa": paper_qa,
            "knowledge_db": knowledge_db,
            "checkpoints_db": checkpoints_db,
            "audit_db": audit_db,
            "model": model,
            "retrieval": retrieval,
            # 真实 reranker 状态报告（配置开关/权重路径/依赖/加载结果/关闭原因/耗时）。
            "reranker": reranker,
            # 完整 retrieval_trace 的读取契约（顺序 + 入口）。
            "retrieval_trace": {
                "enabled": debug_on,
                "order": list(TRACE_ORDER),
                "available_via": (
                    "POST /api/paper-qa/ask {debug:true}（需 SRC_RETRIEVAL_TRACE/"
                    "retrieval.trace.enabled 或本地测试身份 user_id=None）"
                    "；或 GET /api/paper-qa/health?trace_query=...（需 debug 配置）"
                ),
            },
        }
        if trace_query:
            result["retrieval_trace_sample"] = self._health_trace(trace_query, debug_on)
        return result

    def _health_trace(self, query: str, debug_on: bool) -> dict:
        """健康接口的 trace 读取（仅 debug 配置开启时执行）。"""
        if not debug_on:
            return {
                "error": ("debug 未启用：设置 SRC_RETRIEVAL_TRACE=1 或 "
                          "retrieval.trace.enabled=true 后才能通过健康接口读取 trace"),
            }
        try:
            res = self._retriever.search(query, top_k=self.top_k, collect_trace=True)
        except Exception as e:  # noqa: BLE001 - 健康接口不因 trace 失败而 5xx
            return {"error": f"{type(e).__name__}: {e}"}
        return res.trace.to_dict() if res.trace is not None else None

    def _audit_status(self) -> dict:
        try:
            return {"path": self._audit_store().db_path, "events": self._audit_store().count()}
        except Exception as e:
            return {"path": self._audit_db_path, "events": -1, "error": str(e)}

    @staticmethod
    def _validate_question(question: Any) -> str:
        if not isinstance(question, str):
            raise ValueError("question 必须是字符串")
        q = question.strip()
        if not q:
            raise ValueError("问题不能为空，请输入问题内容。")
        return q

    @staticmethod
    def _new_session_id() -> str:
        return f"paper-qa-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
