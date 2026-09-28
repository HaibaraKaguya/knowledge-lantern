"""财务问答三角色的服务端授权测试。"""

from __future__ import annotations

import pytest

from access import Action, IAMStore, initialize_dev_iam
from access.context import AccessContext
from access.decision import REASON_ALLOWED, decide
from access.models import ResourceMetadata


@pytest.fixture()
def iam(tmp_path):
    db = tmp_path / "iam.db"
    initialize_dev_iam(db, force=True)
    store = IAMStore(db)
    yield store
    store.close()


def ctx(iam, user_id):
    return AccessContext.from_user(iam, user_id)


def finance_resource(classification="internal"):
    return ResourceMetadata(
        document_id="finance-close-guide", status="active", classification=classification,
        project_id="finance-ledger", business_domain="finance",
    )


def public_guide():
    return ResourceMetadata(document_id="system-guide", status="active", classification="public")


@pytest.mark.parametrize("user_id", ["employee", "accountant", "finance_supervisor"])
def test_every_role_can_ask_published_knowledge(iam, user_id):
    result = decide(ctx(iam, user_id), Action.INTERNAL_RETRIEVAL_ASK, public_guide())
    assert result.allowed is True


def test_employee_is_limited_to_public_general_content(iam):
    assert decide(ctx(iam, "employee"), Action.INTERNAL_RETRIEVAL_ASK, finance_resource()).allowed is False


def test_accountant_can_access_internal_finance_knowledge(iam):
    result = decide(ctx(iam, "accountant"), Action.INTERNAL_RETRIEVAL_ASK, finance_resource())
    assert result.allowed is True and result.reason == REASON_ALLOWED


def test_finance_supervisor_can_access_restricted_finance_knowledge(iam):
    assert decide(ctx(iam, "finance_supervisor"), Action.INTERNAL_RETRIEVAL_ASK, finance_resource("restricted")).allowed is True


def test_only_accountant_can_submit_knowledge(iam):
    assert Action.KNOWLEDGE_SUBMIT not in ctx(iam, "employee").allowed_actions
    assert Action.KNOWLEDGE_SUBMIT in ctx(iam, "accountant").allowed_actions
    assert Action.KNOWLEDGE_SUBMIT not in ctx(iam, "finance_supervisor").allowed_actions


def test_only_finance_supervisor_can_review_knowledge(iam):
    assert Action.KNOWLEDGE_REVIEW not in ctx(iam, "employee").allowed_actions
    assert Action.KNOWLEDGE_REVIEW not in ctx(iam, "accountant").allowed_actions
    assert Action.KNOWLEDGE_REVIEW in ctx(iam, "finance_supervisor").allowed_actions
