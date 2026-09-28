"""检索过滤契约 — 由 AccessContext 生成安全过滤值（不拼接任何 SQL）。"""

from __future__ import annotations

from .context import AccessContext
from .models import RetrievalFilter
from .scopes import ordered_up_to


def build_retrieval_filter(ctx: AccessContext) -> RetrievalFilter:
    """从授权上下文生成 knowledge.db 检索过滤契约。

    只产出纯值（分类 allow-list + 可选 project/业务域精确集合 + 强制 status='active'），
    由集成方以绑定参数拼进 WHERE。授权层在此绝不产生 SQL 文本。
    """
    allowed_classifications = tuple(c.value for c in ordered_up_to(ctx.classification_ceiling))
    return RetrievalFilter(
        allowed_classifications=allowed_classifications,
        project_ids=(
            None if ctx.project_scopes is None else tuple(sorted(ctx.project_scopes))
        ),
        business_domains=(
            None if ctx.business_domain_scopes is None else tuple(sorted(ctx.business_domain_scopes))
        ),
        status="active",
    )


def _placeholders(n: int) -> str:
    return ",".join("?" for _ in range(n))


def build_document_access_predicate(
    filter_: RetrievalFilter, *, prefix: str = ""
) -> tuple[str, tuple]:
    """构建 documents 表的访问谓词：参数化 WHERE 片段 + 绑定参数。

    语义与 decide() 逐条判定完全一致（fail-closed + 全局公共资源逃逸口）：
    - 用户在某个维度受限制（project_ids / business_domains 非 None）时，该维度上
      "未打标签"（NULL）的资源默认拒绝；
    - 唯一例外是显式建模为"全局可访问公共资源"的资源（classification='public' 且
      project_id 为 NULL），始终放行（公共研究不被阻塞；business_domain 对 public +
      项目未绑定的资源是分类元数据，不构成限制维度）。
    - 未受限的维度不产生约束；未受限的 join 两端同时受限时按 AND 组合。

    只输出 '?' 占位符（所有值走绑定参数），绝不把任何值字符串插值进 SQL 文本。
    prefix 用于限定列名（如 'd.'），避免 JOIN 中列歧义。

    注意：本谓词仅在 documents 表已迁移出 classification / project_id /
    business_domain 列时适用；列缺失（迁移前 legacy 语料）时应由集成方回退为
    status-only 谓词（整个 legacy 语料视为公共语料）。
    """
    clauses = [
        f"{prefix}status = ?",
        f"{prefix}classification IN ({_placeholders(len(filter_.allowed_classifications))})",
    ]
    params: list[object] = [filter_.status, *filter_.allowed_classifications]

    restricted_project = filter_.project_ids is not None
    restricted_domain = filter_.business_domains is not None
    if not restricted_project and not restricted_domain:
        return " AND ".join(clauses), tuple(params)

    # 逃逸口：全局可访问公共资源（public + 项目未绑定）。
    # 与 decide() 的 globally_public 一致：business_domain 对 public + 项目未绑定的
    # 资源不构成限制（真实 public 语料常带业务域标签，不应阻塞公共研究）。
    escape = (
        f"({prefix}classification = 'public' AND {prefix}project_id IS NULL)"
    )
    sub_clauses: list[str] = [escape]

    if restricted_project and restricted_domain:
        compliant = (
            f"({prefix}project_id IN ({_placeholders(len(filter_.project_ids))})"
            f" AND {prefix}business_domain IN ({_placeholders(len(filter_.business_domains))}))"
        )
        params.extend(filter_.project_ids)
        params.extend(filter_.business_domains)
    elif restricted_project:
        compliant = f"{prefix}project_id IN ({_placeholders(len(filter_.project_ids))})"
        params.extend(filter_.project_ids)
    else:
        compliant = f"{prefix}business_domain IN ({_placeholders(len(filter_.business_domains))})"
        params.extend(filter_.business_domains)
    sub_clauses.append(compliant)

    clauses.append("(" + " OR ".join(sub_clauses) + ")")
    return " AND ".join(clauses), tuple(params)
