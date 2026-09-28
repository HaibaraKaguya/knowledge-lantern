"""access 包 — IAM 存储与 schema 测试（三角色最小权限动作集）。"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from access import Classification, IAMStore, InvalidScopeError, UnknownActionError, UnknownUserError  # noqa: E402
from access.actions import Action  # noqa: E402


def make_iam() -> IAMStore:
    return IAMStore()  # 内存库


# ---- schema ----

def test_schema_tables_exist():
    iam = make_iam()
    try:
        rows = iam.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('users','roles','user_roles','user_scopes') ORDER BY name"
        ).fetchall()
        assert [r[0] for r in rows] == ["roles", "user_roles", "user_scopes", "users"]
    finally:
        iam.close()


def test_schema_is_separate_db():
    """IAM 与 knowledge.db 是分离库：knowledge 表不应出现。"""
    iam = make_iam()
    try:
        tables = {r[0] for r in iam.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "documents" not in tables
        assert "chunks" not in tables
        assert "checkpoints" not in tables
    finally:
        iam.close()


# ---- 角色 ----

def test_create_and_get_role():
    iam = make_iam()
    try:
        iam.create_role("accountant", description="只读", actions=(Action.INTERNAL_RETRIEVAL_ASK,))
        role = iam.get_role("accountant")
        assert role is not None
        assert role.role_name == "accountant"
        assert role.actions == (Action.INTERNAL_RETRIEVAL_ASK,)
    finally:
        iam.close()


def test_role_allows_action():
    iam = make_iam()
    try:
        iam.create_role("employee", actions=(Action.INTERNAL_RETRIEVAL_ASK,))
        assert iam.role_allows_action("employee", Action.INTERNAL_RETRIEVAL_ASK) is True
        assert iam.role_allows_action("employee", Action.KNOWLEDGE_SUBMIT) is False
        assert iam.role_allows_action("employee", Action.KNOWLEDGE_REVIEW) is False
        assert iam.role_allows_action("nonexistent", Action.INTERNAL_RETRIEVAL_ASK) is False
        assert iam.role_allows_action("employee", "INTERNAL_RETRIEVAL_ASK") is True
    finally:
        iam.close()


def test_unknown_action_in_role_rejected():
    iam = make_iam()
    try:
        with pytest.raises(UnknownActionError):
            iam.create_role("bad", actions=("ADMIN_ASK",))  # type: ignore[arg-type]
    finally:
        iam.close()


def test_unknown_action_in_stored_json_rejected():
    """roles.actions 中出现未定义动作名 → load_identity fail-closed。"""
    iam = make_iam()
    try:
        iam.create_user("accountant")
        # 直接写入含未定义动作名的角色行并挂到用户（绕过 add_user_role 的提前校验）
        iam.conn.execute(
            "INSERT INTO roles(role_name, actions) VALUES('rogue', ?)",
            ('["INTERNAL_RETRIEVAL_ASK", "ROOT_ASK"]',),
        )
        iam.conn.execute("INSERT INTO user_roles(user_id, role_name) VALUES('accountant', 'rogue')")
        iam.conn.commit()
        with pytest.raises(UnknownActionError):
            iam.load_identity("accountant")
    finally:
        iam.close()


# ---- 用户 ----

def test_create_and_get_user():
    iam = make_iam()
    try:
        iam.create_user("accountant", user_name="财务会计", enabled=True)
        u = iam.get_user("accountant")
        assert u is not None and u.user_name == "财务会计" and u.enabled is True
        assert iam.get_user("nope") is None
    finally:
        iam.close()


def test_add_user_role_requires_existing_entities():
    iam = make_iam()
    try:
        with pytest.raises(Exception):
            iam.add_user_role("ghost", "accountant")
        iam.create_user("accountant")
        with pytest.raises(Exception):
            iam.add_user_role("accountant", "ghost-role")
    finally:
        iam.close()


# ---- 范围 ----

def test_scope_type_validation():
    iam = make_iam()
    try:
        iam.create_user("accountant")
        with pytest.raises(InvalidScopeError):
            iam.add_user_scope("accountant", "department", "eng")
        with pytest.raises(InvalidScopeError):
            iam.add_user_scope("accountant", "classification", "top-secret")
        with pytest.raises(InvalidScopeError):
            iam.add_user_scope("accountant", "project", "")
    finally:
        iam.close()


def test_list_user_scopes():
    iam = make_iam()
    try:
        iam.create_user("accountant")
        iam.add_user_scope("accountant", "project", "finance-ledger")
        iam.add_user_scope("accountant", "business_domain", "finance")
        iam.add_user_scope("accountant", "classification", "internal")
        scopes = iam.list_user_scopes("accountant")
        assert scopes["project"] == frozenset({"finance-ledger"})
        assert scopes["business_domain"] == frozenset({"finance"})
        assert scopes["classification"] == frozenset({"internal"})
    finally:
        iam.close()


# ---- 身份加载 ----

def test_load_identity_fields():
    iam = make_iam()
    try:
        iam.create_role(
            "accountant",
            actions=(Action.INTERNAL_RETRIEVAL_ASK, Action.KNOWLEDGE_SUBMIT),
        )
        iam.create_user("accountant", user_name="财务会计")
        iam.add_user_role("accountant", "accountant")
        iam.add_user_scope("accountant", "project", "finance-ledger")
        iam.add_user_scope("accountant", "business_domain", "finance")
        iam.add_user_scope("accountant", "classification", "internal")
        ident = iam.load_identity("accountant")
        assert ident.user_id == "accountant"
        assert ident.user_name == "财务会计"
        assert ident.roles == ("accountant",)
        assert ident.allowed_actions == frozenset(
            {Action.INTERNAL_RETRIEVAL_ASK, Action.KNOWLEDGE_SUBMIT}
        )
        assert ident.classification_ceiling == Classification.INTERNAL
        assert ident.project_scopes == frozenset({"finance-ledger"})
    finally:
        iam.close()


def test_load_identity_unknown_and_disabled():
    iam = make_iam()
    try:
        with pytest.raises(UnknownUserError):
            iam.load_identity("ghost")
        iam.create_user("U003", enabled=False)
        with pytest.raises(UnknownUserError):
            iam.load_identity("U003")
    finally:
        iam.close()


def test_load_identity_no_scopes_defaults_least_privilege():
    iam = make_iam()
    try:
        iam.create_role(
            "accountant",
            actions=(Action.INTERNAL_RETRIEVAL_ASK, Action.KNOWLEDGE_SUBMIT),
        )
        iam.create_user("U009")
        iam.add_user_role("U009", "accountant")
        ident = iam.load_identity("U009")
        assert ident.classification_ceiling == Classification.PUBLIC  # 最保守
        assert ident.project_scopes is None
        assert ident.business_domain_scopes is None
    finally:
        iam.close()


def test_load_identity_wildcard_dominates_specific():
    iam = make_iam()
    try:
        iam.create_user("U010")
        iam.add_user_scope("U010", "project", "*")
        iam.add_user_scope("U010", "project", "proj-x")
        iam.add_user_scope("U010", "classification", "*")
        ident = iam.load_identity("U010")
        assert ident.project_scopes is None  # '*' 通配 → 不限制
        assert ident.classification_ceiling == Classification.RESTRICTED
    finally:
        iam.close()
