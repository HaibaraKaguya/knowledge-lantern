"""访问控制数据模型（全部为不可变 frozen dataclass）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .actions import Action
from .scopes import Classification

#: 唯一视为"资源启用"的状态值；其余一律拒绝。
ACTIVE_STATUS: Final[str] = "active"


@dataclass(frozen=True)
class Role:
    """角色（actions 为该角色可执行的动作）。"""

    role_name: str
    description: str = ""
    actions: tuple[Action, ...] = ()


@dataclass(frozen=True)
class User:
    """用户主体（不含认证材料）。"""

    user_id: str
    user_name: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class ResourceMetadata:
    """待判定资源的元数据。

    classification 必填（缺失即无法判定，判定函数将 fail-closed 拒绝）。
    status 必填（缺省 None）：仅显式 "active" 视为启用，None/其他一律拒绝——
    防止集成方漏映射状态时 fail-open。
    project_id / business_domain 为 None 表示该资源无此维度作用域（不参与对应维度校验）。
    """

    classification: Classification | str
    status: str | None = None
    project_id: str | None = None
    business_domain: str | None = None
    document_id: str | None = None
    chunk_id: str | None = None


@dataclass(frozen=True)
class Decision:
    """判定结果：allowed + 机器可读 reason 码。"""

    allowed: bool
    reason: str


@dataclass(frozen=True)
class Identity:
    """服务端从 IAM 库加载的不可变身份快照。

    project_scopes / business_domain_scopes: None = 该维度不限制；否则为精确授予值集合。
    classification_ceiling: 最高可访问分类。
    """

    user_id: str
    user_name: str
    enabled: bool
    roles: tuple[str, ...]
    allowed_actions: frozenset[Action]
    project_scopes: frozenset[str] | None
    business_domain_scopes: frozenset[str] | None
    classification_ceiling: Classification


@dataclass(frozen=True)
class RetrievalFilter:
    """knowledge.db 检索的安全过滤契约（纯值，不含任何 SQL 片段/拼接）。

    allowed_classifications: 允许的分类名（升序，均 <= 用户 ceiling）。
    project_ids / business_domains: None = 该维度无约束；
        否则为精确允许的取值集合（调用方必须以绑定参数传入，禁止字符串插值）。
    status: 恒为 'active'（由授权层强制，检索 WHERE 必须带上，禁止放宽）。

    集成方应据此生成参数化 WHERE，例如：
        classification IN (?, ?, ...) AND status = ? [AND project_id IN (?, ...)] ...
    所有值都走绑定参数，绝不把 allowed_classifications / project_ids 直接拼进 SQL 文本。
    """

    allowed_classifications: tuple[str, ...]
    project_ids: tuple[str, ...] | None = None
    business_domains: tuple[str, ...] | None = None
    status: str = ACTIVE_STATUS

    def to_param_dict(self) -> dict[str, object]:
        """以纯字典返回过滤值，便于集成方构建绑定参数。"""
        return {
            "classifications": self.allowed_classifications,
            "project_ids": self.project_ids,
            "business_domains": self.business_domains,
            "status": self.status,
        }
