"""access 包 — 演示身份 seed 测试（显式 opt-in、确定性、幂等、可持久化）。

三角色约定（服务端最小权限）：
- employee（普通员工）：仅公共知识（分类上限 public，项目/业务域不限制），只读检索；
- accountant（财务会计）：可读内部财务知识（finance-ledger/finance，上限 internal），可提交资料；
- finance_supervisor（财务主管）：可读所辖受限财务知识（finance-ledger/finance，上限 restricted），
  可审核发布/拒绝他人资料。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from access import (  # noqa: E402
    DEMO_IDENTITIES,
    AccessContext,
    Classification,
    IAMStore,
    seed_demo_iam,
)


def test_seed_is_opt_in_not_automatic():
    """包导入与 IAMStore 构造都不播种：必须先显式调用 seed_demo_iam。"""
    store = IAMStore()
    try:
        n_users = store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        n_roles = store.conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
        assert n_users == 0
        assert n_roles == 0
    finally:
        store.close()


def test_seed_demo_identities_match_agreed_examples():
    store = IAMStore()
    try:
        counts = seed_demo_iam(store)
        assert counts["users"] == 3
        assert counts["roles"] == 3

        employee = AccessContext.from_user(store, "employee")
        assert employee.user_name == "普通员工"
        assert employee.roles == ("employee",)
        assert employee.project_scopes is None      # 不限制项目
        assert employee.business_domain_scopes is None
        assert employee.classification_ceiling == Classification.PUBLIC

        accountant = AccessContext.from_user(store, "accountant")
        assert accountant.user_name == "财务会计"
        assert accountant.roles == ("accountant",)
        assert accountant.project_scopes == frozenset({"finance-ledger"})
        assert accountant.business_domain_scopes == frozenset({"finance"})
        assert accountant.classification_ceiling == Classification.INTERNAL

        supervisor = AccessContext.from_user(store, "finance_supervisor")
        assert supervisor.user_name == "财务主管"
        assert supervisor.roles == ("finance_supervisor",)
        assert supervisor.project_scopes == frozenset({"finance-ledger"})
        assert supervisor.business_domain_scopes == frozenset({"finance"})
        assert supervisor.classification_ceiling == Classification.RESTRICTED
    finally:
        store.close()


def _table_counts(store: IAMStore) -> dict[str, int]:
    return {
        t: store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("users", "roles", "user_roles", "user_scopes")
    }


def test_seed_is_idempotent():
    store = IAMStore()
    try:
        seed_demo_iam(store)
        first = _table_counts(store)
        counts2 = seed_demo_iam(store)
        second = _table_counts(store)
        assert second == first
        assert counts2["user_roles"] == 0
        assert counts2["user_scopes"] == 0
    finally:
        store.close()


def test_seed_force_rewrites_to_expected_state():
    store = IAMStore()
    try:
        seed_demo_iam(store)
        # 篡改 accountant 的范围/角色，制造漂移
        store.add_user_scope("accountant", "project", "proj-evil")
        store.conn.execute(
            "INSERT INTO user_roles VALUES('accountant','finance_supervisor')"
        )
        store.conn.commit()
        drifted = AccessContext.from_user(store, "accountant")
        assert "finance_supervisor" in drifted.roles

        seed_demo_iam(store, force=True)
        restored = AccessContext.from_user(store, "accountant")
        assert restored.project_scopes == frozenset({"finance-ledger"})
        assert restored.roles == ("accountant",)
        assert restored.classification_ceiling == Classification.INTERNAL
    finally:
        store.close()


def test_seed_persists_to_file_db(tmp_path):
    """seed 写入调用方提供的独立 IAM 库文件，重开后可读。"""
    db_path = tmp_path / "iam.db"
    store = IAMStore(db_path)
    try:
        seed_demo_iam(store)
    finally:
        store.close()

    reopened = IAMStore(db_path)
    try:
        u = AccessContext.from_user(reopened, "accountant")
        assert u.user_name == "财务会计"
        assert u.classification_ceiling == Classification.INTERNAL
        # IAM 库中不含 knowledge.db 的语料表（库分离）
        tables = {r[0] for r in reopened.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "documents" not in tables
    finally:
        reopened.close()


def test_demo_identities_constant_is_stable():
    assert [(d.user_id, d.user_name) for d in DEMO_IDENTITIES] == [
        ("employee", "普通员工"),
        ("accountant", "财务会计"),
        ("finance_supervisor", "财务主管"),
    ]
