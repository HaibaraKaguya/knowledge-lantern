"""Knowledge Lantern 访问控制基础包。

只依赖标准库 sqlite3，授权判定中不含任何 LLM/Agent。职责：
- 在调用方提供的独立 IAM 库（如 iam.db）中维护 users / roles / user_roles / user_scopes；
- 服务端从 user_id 构建不可变 AccessContext（拒绝客户端伪造的 role/scope JSON）；
- 确定性判定 decide(ctx, action, resource) 返回 allow/deny + 机器可读 reason；
- 为 knowledge.db 检索提供安全过滤契约（纯值，不拼接 SQL）。

认证（登录、会话、密钥）不在本包范围；调用方传入已完成认证的 user_id。
"""

from __future__ import annotations

from .actions import ALL_ACTIONS, Action
from .context import AccessContext
from .decision import decide
from .filter import build_document_access_predicate, build_retrieval_filter
from .iam import IAMError, IAMStore, InvalidScopeError, UnknownActionError, UnknownUserError
from .models import Decision, Identity, ResourceMetadata, RetrievalFilter, Role, User
from .scopes import Classification, SCOPE_TYPES, WILDCARD
from .seed import DEMO_IDENTITIES, initialize_dev_iam, seed_demo_iam

__all__ = [
    "Action",
    "ALL_ACTIONS",
    "Classification",
    "SCOPE_TYPES",
    "WILDCARD",
    "IAMStore",
    "IAMError",
    "UnknownUserError",
    "UnknownActionError",
    "InvalidScopeError",
    "AccessContext",
    "ResourceMetadata",
    "Decision",
    "RetrievalFilter",
    "Identity",
    "Role",
    "User",
    "decide",
    "build_retrieval_filter",
    "build_document_access_predicate",
    "seed_demo_iam",
    "initialize_dev_iam",
    "DEMO_IDENTITIES",
]
