"""AccessContext — 不可变、服务端生成的授权上下文。

安全边界：AccessContext 只能通过 from_user() 从 IAMStore（服务端数据库）
构建。frozen dataclass 保证构建后不可修改；判定函数只接受 AccessContext
实例，客户端伪造的 dict / JSON 无法直接进入判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .actions import Action
from .scopes import Classification

if TYPE_CHECKING:
    from .iam import IAMStore


@dataclass(frozen=True)
class AccessContext:
    """服务端从 user_id 生成的不可变授权上下文。

    字段语义与 Identity 一致：
    - project_scopes / business_domain_scopes: None = 该维度不限制；否则精确授予值集合；
    - classification_ceiling: 最高可访问分类。
    """

    user_id: str
    user_name: str
    roles: tuple[str, ...]
    allowed_actions: frozenset[Action]
    project_scopes: frozenset[str] | None
    business_domain_scopes: frozenset[str] | None
    classification_ceiling: Classification

    @classmethod
    def from_user(cls, iam: "IAMStore", user_id: str) -> "AccessContext":
        """服务端身份构建（唯一受支持路径）。

        只信任 IAMStore 中的数据库记录。即使调用方收到客户端声称的 role/scope
        JSON，也必须只提取 user_id 调用本方法；客户端伪造的角色与范围不会进入判定。
        """
        identity = iam.load_identity(user_id)
        return cls(
            user_id=identity.user_id,
            user_name=identity.user_name,
            roles=identity.roles,
            allowed_actions=frozenset(identity.allowed_actions),
            project_scopes=identity.project_scopes,
            business_domain_scopes=identity.business_domain_scopes,
            classification_ceiling=identity.classification_ceiling,
        )
