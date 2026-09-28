"""演示身份播种 — 显式、opt-in。

seed_demo_iam() 只在调用方显式调用时才写入 IAM 库；包导入与 IAMStore
构造均不会自动播种。幂等：默认 INSERT OR IGNORE（跳过已存在记录）；
force=True 时重写演示用户的角色与范围（角色/用户本身恒为 upsert）。

约定演示身份（确定性）—— 三角色最小权限：
- employee（普通员工）           ：只能检索全公司公开知识（分类上限 public，项目/业务域不限制）；
- accountant（财务会计）         ：可检索公共 + 内部财务知识（finance-ledger/finance，上限 internal），并可提交资料；
- finance_supervisor（财务主管） ：可检索公共 + 内部 + 受限财务知识（finance-ledger/finance，上限 restricted），
                                   可审核并发布/拒绝他人提交的资料，但不能伪造审核人、不能审核自己的资料。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .actions import Action
from .iam import IAMStore
from .scopes import WILDCARD


@dataclass(frozen=True)
class DemoRole:
    role_name: str
    description: str
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class DemoIdentity:
    user_id: str
    user_name: str
    roles: tuple[str, ...]
    project_scopes: tuple[str, ...]
    business_domain_scopes: tuple[str, ...]
    classification_ceiling: str


#: 演示角色定义（确定性）。
DEMO_ROLES: tuple[DemoRole, ...] = (
    DemoRole(
        "employee", "普通员工：可提问全公司公开知识，只能读写自己的会话",
        (Action.INTERNAL_RETRIEVAL_ASK,),
    ),
    DemoRole(
        "accountant", "财务会计：公共与内部财务知识问答 + 提交资料",
        (Action.INTERNAL_RETRIEVAL_ASK, Action.KNOWLEDGE_SUBMIT),
    ),
    DemoRole(
        "finance_supervisor", "财务主管：所辖受限财务知识问答 + 审核/发布资料",
        (Action.INTERNAL_RETRIEVAL_ASK, Action.KNOWLEDGE_REVIEW),
    ),
)

#: 演示身份（确定性）。
DEMO_IDENTITIES: tuple[DemoIdentity, ...] = (
    DemoIdentity(
        user_id="employee",
        user_name="普通员工",
        roles=("employee",),
        # 空元组 = 该维度不限制：员工可检索所有公开分类的资料（跨部门/跨项目），
        # 但分类上限 public 保证内部/受限财务知识不可见。
        project_scopes=(),
        business_domain_scopes=(),
        classification_ceiling="public",
    ),
    DemoIdentity(
        user_id="accountant",
        user_name="财务会计",
        roles=("accountant",),
        project_scopes=("finance-ledger",),
        business_domain_scopes=("finance",),
        classification_ceiling="internal",
    ),
    DemoIdentity(
        user_id="finance_supervisor",
        user_name="财务主管",
        roles=("finance_supervisor",),
        project_scopes=("finance-ledger",),
        business_domain_scopes=("finance",),
        classification_ceiling="restricted",
    ),
)


def seed_demo_iam(iam: IAMStore, *, force: bool = False) -> dict[str, int]:
    """播种演示角色与身份（幂等；不调用则不写入）。

    force=True 时，对演示用户先清除再重写其 user_roles / user_scopes，
    保证与 DEMO_IDENTITIES 完全一致；force=False 时跳过已存在记录。
    返回各类写入计数。
    """
    counts: dict[str, int] = {"roles": 0, "users": 0, "user_roles": 0, "user_scopes": 0}

    for role in DEMO_ROLES:
        iam.create_role(role.role_name, description=role.description, actions=role.actions)
        counts["roles"] += 1

    for ident in DEMO_IDENTITIES:
        iam.create_user(ident.user_id, user_name=ident.user_name, enabled=True)
        counts["users"] += 1

        if force:
            iam.conn.execute(
                "DELETE FROM user_roles WHERE user_id = ?", (ident.user_id,)
            )
            iam.conn.execute(
                "DELETE FROM user_scopes WHERE user_id = ?", (ident.user_id,)
            )
            iam.conn.commit()

        for role_name in ident.roles:
            before = _count_user_roles(iam.conn, ident.user_id)
            iam.add_user_role(ident.user_id, role_name)
            if _count_user_roles(iam.conn, ident.user_id) > before:
                counts["user_roles"] += 1

        scope_rows: list[tuple[str, str]] = []
        scope_rows += [("project", v) for v in ident.project_scopes]
        scope_rows += [("business_domain", v) for v in ident.business_domain_scopes]
        scope_rows.append(("classification", ident.classification_ceiling))
        for scope_type, scope_value in scope_rows:
            before = _scope_count(iam.conn, ident.user_id, scope_type, scope_value)
            iam.add_user_scope(ident.user_id, scope_type, scope_value)
            if _scope_count(iam.conn, ident.user_id, scope_type, scope_value) > before:
                counts["user_scopes"] += 1

    return counts


def _count_user_roles(conn, user_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM user_roles WHERE user_id = ?", (user_id,)
    ).fetchone()[0]


def _scope_count(conn, user_id: str, scope_type: str, scope_value: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM user_scopes WHERE user_id = ? AND scope_type = ? AND scope_value = ?",
        (user_id, scope_type, scope_value),
    ).fetchone()[0]


def initialize_dev_iam(
    db_path: str | Path, *, force: bool = False
) -> dict[str, int]:
    """显式、opt-in 的开发初始化：在 db_path 建库并用演示身份播种。

    这是"提供 data/iam.db 开发初始化"的唯一受支持入口——必须由调用方显式调用
    （CLI 或服务端启动脚本），包导入与 IAMStore 构造均不会自动播种。
    幂等：force=False 时跳过已存在记录；force=True 时重写演示用户的角色与范围。

    返回值同 seed_demo_iam：各类写入计数。
    """
    from .iam import IAMStore

    store = IAMStore(db_path)
    try:
        return seed_demo_iam(store, force=force)
    finally:
        store.close()


def demo_roles_json() -> str:
    """演示角色定义的 JSON 表达（报告/文档用，不含任何密钥）。"""
    return json.dumps(
        [
            {"role_name": r.role_name, "description": r.description, "actions": [str(a) for a in r.actions]}
            for r in DEMO_ROLES
        ],
        ensure_ascii=False,
        indent=2,
    )
