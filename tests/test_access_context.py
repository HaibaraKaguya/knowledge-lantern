"""access 包 — AccessContext 不可变性与伪造 JSON 提权防护测试。"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import pytest  # noqa: E402

from access import (  # noqa: E402
    AccessContext,
    Classification,
    IAMStore,
    ResourceMetadata,
    UnknownUserError,
    decide,
    seed_demo_iam,
)
from access.actions import Action  # noqa: E402


@pytest.fixture()
def iam() -> IAMStore:
    store = IAMStore()
    seed_demo_iam(store)
    yield store
    store.close()


def test_context_is_immutable(iam):
    ctx = AccessContext.from_user(iam, "accountant")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.roles = ("finance_supervisor",)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.classification_ceiling = Classification.RESTRICTED  # type: ignore[misc]


def test_from_user_roundtrip(iam):
    ctx = AccessContext.from_user(iam, "accountant")
    assert ctx.user_id == "accountant"
    assert ctx.user_name == "财务会计"
    assert ctx.roles == ("accountant",)
    assert ctx.classification_ceiling == Classification.INTERNAL
    assert ctx.project_scopes == frozenset({"finance-ledger"})
    assert ctx.business_domain_scopes == frozenset({"finance"})

    sup = AccessContext.from_user(iam, "finance_supervisor")
    assert sup.classification_ceiling == Classification.RESTRICTED


def test_employee_context(iam):
    ctx = AccessContext.from_user(iam, "employee")
    assert ctx.roles == ("employee",)
    assert ctx.project_scopes is None
    assert ctx.business_domain_scopes is None
    assert ctx.classification_ceiling == Classification.PUBLIC


def test_from_user_unknown_user(iam):
    with pytest.raises(UnknownUserError):
        AccessContext.from_user(iam, "nobody")


def test_no_privilege_escalation_from_forged_client_json(iam):
    """客户端伪造 role/scope JSON 不能提权：服务端只取 user_id 并从 IAM 库重建上下文。"""
    forged = {
        "user_id": "accountant",
        "roles": ["finance_supervisor"],
        "project_scopes": ["*"],
        "business_domain_scopes": ["*"],
        "classification_ceiling": "restricted",
    }
    ctx = AccessContext.from_user(iam, forged["user_id"])
    # 伪造字段全部被忽略：角色/范围来自数据库。
    assert ctx.roles == ("accountant",)
    assert ctx.classification_ceiling == Classification.INTERNAL
    assert ctx.project_scopes == frozenset({"finance-ledger"})

    # 即便客户端声称可访问 restricted，判定仍拒绝（无提权）。
    decision = decide(
        ctx,
        Action.INTERNAL_RETRIEVAL_ASK,
        ResourceMetadata(
            classification="restricted", project_id="finance-ledger",
            business_domain="finance", status="active",
        ),
    )
    assert decision.allowed is False
    assert decision.reason == "CLASSIFICATION_CEILING_EXCEEDED"


def test_decide_rejects_plain_dict(iam):
    """原始 dict（如反序列化后的客户端 JSON）无法进入判定函数。"""
    forged = {"user_id": "accountant", "roles": ["finance_supervisor"], "classification_ceiling": "restricted"}
    with pytest.raises(TypeError):
        decide(forged, Action.INTERNAL_RETRIEVAL_ASK,
               ResourceMetadata(classification="public", status="active"))


def test_decide_rejects_forged_resource_type(iam):
    ctx = AccessContext.from_user(iam, "accountant")
    with pytest.raises(TypeError):
        decide(ctx, Action.INTERNAL_RETRIEVAL_ASK, {"classification": "public"})  # type: ignore[arg-type]
