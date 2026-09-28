"""Knowledge QA 后端 — 只读知识库检索 + 模型服务生成 + 会话持久化.

与旧多 Agent 编排器完全隔离：本包只支持 INTERNAL_RETRIEVAL 一条产品路径
（财务与部门知识问答）。检索走 SQLite FTS5，生成走 OpenAI-compatible
OpenAI 兼容端点），会话与审计各自独立落库。
"""

from .audit import AuditStore, new_request_id
from .conversation import (
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationStore,
)
from .llm import AiunavilableError, PaperAnswerGenerator, extract_text
from .retrieval import ReadOnlyPaperRetriever, RetrievedChunk, RetrievalResult
from .rewrite import QueryRewriter
from .router import (
    DirectAnswerer,
    RouteClassifier,
    ROUTE_DIRECT,
    ROUTE_INTERNAL_RETRIEVAL,
)
from .service import AccessDeniedError, PaperQAService

__all__ = [
    "AuditStore",
    "new_request_id",
    "ConversationStore",
    "ConversationNotFoundError",
    "ConversationOwnershipError",
    "PaperAnswerGenerator",
    "AiunavilableError",
    "extract_text",
    "ReadOnlyPaperRetriever",
    "RetrievedChunk",
    "RetrievalResult",
    "QueryRewriter",
    "RouteClassifier",
    "DirectAnswerer",
    "ROUTE_INTERNAL_RETRIEVAL",
    "ROUTE_DIRECT",
    "AccessDeniedError",
    "PaperQAService",
]
