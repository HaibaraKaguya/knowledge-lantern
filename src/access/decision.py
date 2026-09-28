"""确定性授权判定函数 decide()。

判定是纯函数、无副作用、无 LLM/Agent。评估顺序固定（reason 码因此可复现）：
1. 动作不在上下文允许集合 → ACTION_NOT_PERMITTED；
2. INTERNAL_RETRIEVAL_ASK 缺资源 → RESOURCE_MISSING；
4. 资源状态非显式 'active'（含 None/缺省）→ RESOURCE_INACTIVE；
5. 资源分类无法识别 → INVALID_RESOURCE_CLASSIFICATION（fail-closed）；
6. 资源分类超过用户 ceiling → CLASSIFICATION_CEILING_EXCEEDED；
7. 项目维度越界 → PROJECT_SCOPE_DENIED；
8. 业务域维度越界 → BUSINESS_DOMAIN_SCOPE_DENIED；
否则 ALLOWED。

范围维度的 fail-closed 规则：当用户的 project / business_domain 受限制（非 None）时，
该维度上"未打标签"（值为 None）的资源一律拒绝，除非该资源被显式建模为
"全局可访问公共资源"（classification='public' 且 project_id 为空）——公共资源不受
受限用户的 grants 意外阻塞，公共研究始终可检索。public + 项目未绑定的资源，其
business_domain 视为分类元数据而非限制维度（真实 public 语料常带业务域标签）。
"""

from __future__ import annotations

from .actions import Action
from .context import AccessContext
from .models import ACTIVE_STATUS, Decision, ResourceMetadata
from .scopes import Classification, coerce_classification

#: 机器可读 reason 码（Kebab/Snake 大写下划线，稳定契约）。
REASON_ALLOWED = "ALLOWED"
REASON_INVALID_ACTION = "INVALID_ACTION"
REASON_ACTION_NOT_PERMITTED = "ACTION_NOT_PERMITTED"
REASON_RESOURCE_MISSING = "RESOURCE_MISSING"
REASON_RESOURCE_INACTIVE = "RESOURCE_INACTIVE"
REASON_INVALID_RESOURCE_CLASSIFICATION = "INVALID_RESOURCE_CLASSIFICATION"
REASON_CLASSIFICATION_CEILING_EXCEEDED = "CLASSIFICATION_CEILING_EXCEEDED"
REASON_PROJECT_SCOPE_DENIED = "PROJECT_SCOPE_DENIED"
REASON_BUSINESS_DOMAIN_SCOPE_DENIED = "BUSINESS_DOMAIN_SCOPE_DENIED"


def decide(
    ctx: AccessContext,
    action: Action | str,
    resource: ResourceMetadata | None = None,
) -> Decision:
    """对 (上下文, 动作, 资源) 做确定性判定。返回 Decision(allowed, reason)。"""
    if not isinstance(ctx, AccessContext):
        raise TypeError("decide() 只接受 AccessContext（必须由服务端 from_user() 构建）")
    if isinstance(action, str):
        try:
            action = Action(action)
        except ValueError:
            return Decision(False, REASON_INVALID_ACTION)
    if not isinstance(action, Action):
        raise TypeError(f"action 必须是 Action 或动作名，收到: {type(action).__name__}")

    if action not in ctx.allowed_actions:
        return Decision(False, REASON_ACTION_NOT_PERMITTED)

    # 唯一的知识检索动作：必须携带资源元数据。
    if resource is None:
        return Decision(False, REASON_RESOURCE_MISSING)
    if not isinstance(resource, ResourceMetadata):
        raise TypeError("resource 必须是 ResourceMetadata")

    if resource.status != ACTIVE_STATUS:
        return Decision(False, REASON_RESOURCE_INACTIVE)

    classification = coerce_classification(resource.classification)
    if classification is None:
        return Decision(False, REASON_INVALID_RESOURCE_CLASSIFICATION)
    if classification.rank > ctx.classification_ceiling.rank:
        return Decision(False, REASON_CLASSIFICATION_CEILING_EXCEEDED)

    # 全局可访问公共资源逃逸口：classification='public' 且未绑定项目（project_id 为空）。
    # 这类资源对所有用户可访问（公共研究不被受限用户的 grants 意外阻塞）。
    # business_domain 对 public + 项目未绑定的资源是分类元数据而非限制维度：
# 真实语料（public 语料）常带 research/engineering 等业务域标签，不应因此被
# 受限用户（如会计仅 finance 范围）意外挡在公共知识之外。
    globally_public = (
        classification == Classification.PUBLIC
        and resource.project_id is None
    )

    if ctx.project_scopes is not None and not globally_public:
        # 受限用户 + 非全局公共资源：project 未打标签或不在 allow-list → fail-closed 拒绝。
        if resource.project_id is None or resource.project_id not in ctx.project_scopes:
            return Decision(False, REASON_PROJECT_SCOPE_DENIED)

    if ctx.business_domain_scopes is not None and not globally_public:
        # 受限用户 + 非全局公共资源：business_domain 未打标签或不在 allow-list → fail-closed 拒绝。
        if (
            resource.business_domain is None
            or resource.business_domain not in ctx.business_domain_scopes
        ):
            return Decision(False, REASON_BUSINESS_DOMAIN_SCOPE_DENIED)

    return Decision(True, REASON_ALLOWED)
