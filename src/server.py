"""Knowledge Lantern — 财务与部门知识问答 FastAPI 服务.

══════════════════════════════════════════════════════════════════════
端点总览
══════════════════════════════════════════════════════════════════════
  GET  /                      — Web UI（static/index.html，前端 8000）
  GET  /api/health            — 进程健康检查
  GET  /api/paper-qa/health   — 知识库只读计数 + 会话/审计计数 + 模型服务探测
                                + 真实 reranker 状态；?trace_query= 在 debug 配置下可读 trace
  GET  /api/paper-qa/identities — demo 身份发现（本地选择契约，无认证）
  POST /api/paper-qa/ask      — 自动路由问答（检索证据 / DIRECT 直接作答，先授权后路由）
  POST /api/paper-qa/conversations — 创建由 user_id 拥有的服务端会话
  GET  /api/paper-qa/conversations — 列出 user_id 拥有的会话（归属过滤）
  GET  /api/paper-qa/conversations/{conversation_id} — 读会话 + 轮次（归属校验）
  GET  /api/knowledge/documents — 列出资料元数据（不含正文），可 ?status= 过滤
  POST /api/knowledge/ingest    — 会计提交资料进入待审核区（服务端强制角色）
  POST /api/knowledge/documents/{id}/publish — 主管审核发布（不能审核自己提交）
  POST /api/knowledge/documents/{id}/reject  — 主管审核拒绝（同上）
  GET  /api/study/ocr/health    — 学习 OCR 适配器 + 外部 LearningOCR 能力状态（mock/real）
  POST /api/study/ocr/uploads   — 上传图片到受控目录，返回相对引用
  POST /api/study/ocr/jobs      — 提交处理请求（受控图片引用或已存在记录，二选一）
  GET  /api/study/ocr/jobs      — 列出处理任务元数据（不含图片/OCR 原文）
  GET  /api/study/ocr/jobs/{id} — 读取结构化结果（主记录 + 审计 sidecar + 校验）

调用链：前端 8000 → FastAPI（本文件）→ src/access（三角色最小权限）
→ src/paper_qa.service（SQLite 检索/证据 + 会话 + 审计）→ OpenAI-compatible 模型服务。
问题可经 request_id / 服务端日志 / retrieval_query / sources 定位。

已物理移除的旧物：
  - LangGraph 多 Agent 工作流 HTTP 入口（/api/run、/api/run/stream）
  - WebSocket 实时进度（/ws/{thread_id}）与 ConnectionManager
  - 旧会话路由（/api/sessions、/api/sessions/{thread_id}/history）
  - skill 查询（/api/skills）、constitution（/api/constitution）、旧页面托管（/api/pages）
这些路由不再注册，调用方（static/ 前端）已不依赖任何一项；请求将得到 404。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ── 防御 BrokenPipeError：uvicorn reload 时 stdout 管道可能断开 ──
class _SafeStdout:
    def __init__(self, stream):
        self._stream = stream
    def write(self, data):
        try:
            return self._stream.write(data)
        except BrokenPipeError:
            return 0
    def flush(self):
        try:
            self._stream.flush()
        except BrokenPipeError:
            pass
    def __getattr__(self, name):
        return getattr(self._stream, name)

sys.stdout = _SafeStdout(sys.stdout)
sys.stderr = _SafeStdout(sys.stderr)

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.paper_qa.conversation import (
    ConversationNotFoundError,
    ConversationOwnershipError,
)
from src.paper_qa.material import (
    MaterialError,
    list_pending_documents,
    review_material,
    submit_material,
)
from src.paper_qa.schemas import (
    KnowledgeIngestRequest,
    KnowledgeReviewRequest,
    PaperQAAskRequest,
    PaperQAConversationCreateRequest,
)
from src.paper_qa.router import DirectAnswerer, RouteClassifier
from src.paper_qa.service import AccessDeniedError, PaperQAService
from src.study.ocr_api import create_study_ocr_router
from src.utils.files import DATA_DIR
from access.iam import UnknownUserError

logger = logging.getLogger("knowledge_lantern.server")


# ═══════════════════════════════════════════════════════════════════
# 日志配置：同时输出控制台与文件（data/knowledge-lantern.log）
# ═══════════════════════════════════════════════════════════════════
_LOG_PATH = os.path.join(DATA_DIR, "knowledge-lantern.log")
_LOGGER_INITIALIZED = False


def _setup_logging() -> None:
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return
    _LOGGER_INITIALIZED = True
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(fh)
    root.addHandler(ch)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

#python处理不同文件的时候的做法，用于和类型不同的文件做链接***
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：初始化日志 + 强制把演示用户角色/范围同步到 IAM 库（防漂移）。"""
    _setup_logging()
    from access.seed import initialize_dev_iam
    initialize_dev_iam(Path(DATA_DIR) / "iam.db", force=True)
    logger.info("Knowledge Lantern service started")
    logger.info("Log file: %s", _LOG_PATH)
    yield


app = FastAPI(title="Knowledge Lantern", version="3.0.0", lifespan=_lifespan)


# ═══════════════════════════════════════════════════════════════════
# 错误 → HTTP 语义
# ═══════════════════════════════════════════════════════════════════
#   401 — 未知 demo 身份
#   403 — 授权拒绝（动作未授权 / 越权分类 / 审自己 / 跨用户会话）
#   404 — 会话/文档不存在
#   400 — 输入校验失败（非法分类/部门/空内容/审核人不一致）
#   409 — 状态冲突（非 pending 资料不可审核）
#   evidence 不足（insufficient_evidence）恒为 200，与 denied access 严格区分。

@app.exception_handler(AccessDeniedError)
async def _access_denied_handler(request, exc: AccessDeniedError):
    return JSONResponse(status_code=403, content={
        "error": "access_denied",
        "detail": exc.reason,
        "reason": exc.reason,
        "request_id": exc.request_id,
        "user_id": exc.user_id,
        "conversation_id": exc.conversation_id,
        "route": exc.route,
    })


@app.exception_handler(MaterialError)
async def _material_error_handler(request, exc: MaterialError):
    status = {
        "ACTION_NOT_PERMITTED": 403,
        "CLASSIFICATION_EXCEEDS_CEILING": 403,
        "SELF_REVIEW_DENIED": 403,
        "INVALID_REVIEW_ACTION": 400,
        "INVALID_CLASSIFICATION": 400,
        "INVALID_DEPARTMENT": 400,
        "EMPTY_TITLE": 400,
        "EMPTY_CONTENT": 400,
        "DOCUMENT_NOT_FOUND": 404,
        "NOT_PENDING_REVIEW": 409,
    }.get(exc.reason, 400)
    return JSONResponse(status_code=status, content={
        "error": "material_" + exc.reason.lower(),
        "detail": exc.detail or exc.reason,
        "reason": exc.reason,
        "user_id": exc.user_id,
        "document_id": exc.document_id,
    })


@app.exception_handler(ConversationOwnershipError)
async def _conversation_ownership_handler(request, exc: ConversationOwnershipError):
    return JSONResponse(status_code=403, content={
        "error": "conversation_ownership_denied",
        "detail": "会话不属于请求用户",
        "conversation_id": exc.args[0] if exc.args else None,
    })


@app.exception_handler(ConversationNotFoundError)
async def _conversation_not_found_handler(request, exc: ConversationNotFoundError):
    return JSONResponse(status_code=404, content={
        "error": "conversation_not_found",
        "detail": "会话不存在",
        "conversation_id": exc.args[0] if exc.args else None,
    })


@app.exception_handler(UnknownUserError)
async def _unknown_user_handler(request, exc: UnknownUserError):
    return JSONResponse(status_code=401, content={
        "error": "unknown_user",
        "detail": f"未知演示身份: {exc}",
    })


# ═══════════════════════════════════════════════════════════════════
# CORS（默认同源；生产经 CORS_ORIGINS 环境变量收紧）
# ═══════════════════════════════════════════════════════════════════
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ── 服务（懒加载：构造不触库；检索每次打开只读短连接）──────────
# 生产装配显式注入自动路由器 + DIRECT 作答器：路由器用模型服务在
# INTERNAL_RETRIEVAL 与 DIRECT 间选择，非法/失败一律回退检索路径。
# 路由与作答客户端均为懒加载（首次调用才连接模型服务），导入阶段不触网。
_paper_qa_service = PaperQAService(
    router=RouteClassifier(),
    direct_answerer=DirectAnswerer(),
)


# ═══════════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════════

def _knowledge_db_path() -> str:
    return str(Path(DATA_DIR) / "knowledge.db")


def _iam_db_path() -> str:
    return str(Path(DATA_DIR) / "iam.db")


@app.get("/api/health")
async def health():
    """进程健康检查。"""
    return {"status": "ok", "service": "Knowledge Lantern"}


@app.get("/api/paper-qa/health")
def paper_qa_health(trace_query: str | None = None):
    """部门知识问答健康检查：知识库只读计数 + 会话/审计计数 + 模型服务探测 + reranker 状态。

    ?trace_query=... 在显式开启 debug 配置（SRC_RETRIEVAL_TRACE/retrieval.trace.enabled）
    时额外返回一次完整 retrieval_trace；未开启时只返回提示，不执行检索。
    """
    if trace_query:
        return _paper_qa_service.health(trace_query=trace_query)
    return _paper_qa_service.health()


@app.get("/api/paper-qa/identities")
def paper_qa_identities():
    """demo 身份发现（无认证：客户端以 user_id 显式选择，服务端从 iam.db 解析）。"""
    return {
        "contract": (
            "本地开发无真实认证：客户端在 user_id 字段显式选择下列 demo 身份，"
            "服务端从 iam.db 解析其不可变权限，绝不信任客户端伪造的 role/scope。"
        ),
        "identities": _paper_qa_service.list_demo_identities(),
    }


@app.post("/api/paper-qa/ask")
def paper_qa_ask(req: PaperQAAskRequest):
    """自动路由问答：模型路由器选 INTERNAL_RETRIEVAL（检索+证据）或 DIRECT（直接作答）。

    DIRECT 不检索知识库、sources 恒为空；两种路径都复用 user_id 身份解析、
    会话归属校验、会话持久化与审计。route 由服务端自动决定，用户不可手选
    （请求带 route 等未知字段 → 422）。

    debug=true 请求完整 retrieval_trace，但仅当服务端开启 debug 配置
    （SRC_RETRIEVAL_TRACE/retrieval.trace.enabled）或调用为本地测试身份时返回；
    普通用户默认响应保持简洁（无 trace）。
    """
    try:
        return _paper_qa_service.ask(
            req.question, user_id=req.user_id,
            conversation_id=req.conversation_id, session_id=req.session_id,
            debug=req.debug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/paper-qa/conversations")
def paper_qa_create_conversation(req: PaperQAConversationCreateRequest):
    """创建由 user_id 拥有的服务端会话，返回 conversation_id。"""
    return _paper_qa_service.create_conversation(req.user_id)


@app.get("/api/paper-qa/conversations")
def paper_qa_list_conversations(user_id: str):
    """列出 user_id 拥有的会话（归属过滤，看不到他人会话）。"""
    return {"conversations": _paper_qa_service.list_conversations(user_id)}


@app.get("/api/paper-qa/conversations/{conversation_id}")
def paper_qa_get_conversation(conversation_id: str, user_id: str):
    """读取会话 + 轮次；归属不符 → 403，不存在 → 404。"""
    return _paper_qa_service.get_conversation(conversation_id, user_id)


@app.get("/api/knowledge/documents")
def knowledge_documents(status: str | None = None):
    """列出资料元数据（不含正文），可 ?status= 过滤。"""
    return {"documents": list_pending_documents(_knowledge_db_path(), status=status)}


@app.post("/api/knowledge/ingest")
def knowledge_ingest(req: KnowledgeIngestRequest):
    """会计提交资料进入待审核区（不经审核不进入检索）。"""
    result = submit_material(
        _knowledge_db_path(), _iam_db_path(),
        user_id=req.user_id, title=req.title, content=req.content,
        classification=req.classification, department=req.department,
        document_id=req.document_id, version=req.version, source=req.source,
    )
    logger.info(
        "knowledge ingest ok: user=%s document_id=%s classification=%s status=pending_review",
        req.user_id, result["document_id"], req.classification,
    )
    return result


@app.post("/api/knowledge/documents/{document_id}/publish")
def knowledge_publish(document_id: str, req: KnowledgeReviewRequest):
    """主管审核并发布资料；发布者不能是原提交者、审核人不可伪造。"""
    result = review_material(
        _knowledge_db_path(), _iam_db_path(),
        document_id=document_id, user_id=req.user_id,
        action="publish", note=req.note,
    )
    logger.info("knowledge publish ok: reviewer=%s document_id=%s", req.user_id, document_id)
    return result


@app.post("/api/knowledge/documents/{document_id}/reject")
def knowledge_reject(document_id: str, req: KnowledgeReviewRequest):
    """主管审核并拒绝资料；审核人不可伪造、不能审自己的资料。"""
    result = review_material(
        _knowledge_db_path(), _iam_db_path(),
        document_id=document_id, user_id=req.user_id,
        action="reject", note=req.note,
    )
    logger.info("knowledge reject ok: reviewer=%s document_id=%s", req.user_id, document_id)
    return result


# ═══════════════════════════════════════════════════════════════════
# 学习模块 OCR 接入（/api/study/ocr/*）
# ═══════════════════════════════════════════════════════════════════
# 适配层只通过子进程调用外部 LearningOCR 项目（不复制模型权重、不复制 .venv），
# 图片引用被限制在受控上传目录内，禁止任意文件读取。装配在导入阶段不触网、
# 不启动子进程（能力探测在首次 /health 调用时才发生）。
app.include_router(create_study_ocr_router())


# ═══════════════════════════════════════════════════════════════════
# 静态文件（前端 8000）
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    uvicorn.run("src.server:app", host="0.0.0.0", port=8000, reload=True)
