"""access 包 — 检索过滤契约（RetrievalFilter 值）测试。"""

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
    IAMStore,
    build_document_access_predicate,
    build_retrieval_filter,
    seed_demo_iam,
)
from access.actions import Action  # noqa: E402
from access.decision import decide  # noqa: E402
from access.models import ResourceMetadata  # noqa: E402


@pytest.fixture()
def iam() -> IAMStore:
    store = IAMStore()
    seed_demo_iam(store)
    yield store
    store.close()


def test_employee_filter_values(iam):
    """employee（public 上限，项目/业务域不限制）→ 全项目公共知识。"""
    ctx = AccessContext.from_user(iam, "employee")
    filt = build_retrieval_filter(ctx)
    assert filt.allowed_classifications == ("public",)
    assert filt.project_ids is None
    assert filt.business_domains is None
    assert filt.status == "active"  # 授权层强制，检索不得放宽


def test_accountant_filter_values(iam):
    """accountant（finance-ledger/finance，ceiling=internal）→ 精确契约值。"""
    ctx = AccessContext.from_user(iam, "accountant")
    filt = build_retrieval_filter(ctx)
    assert filt.allowed_classifications == ("public", "internal")
    assert filt.project_ids == ("finance-ledger",)
    assert filt.business_domains == ("finance",)
    assert filt.status == "active"


def test_supervisor_filter_values(iam):
    """finance_supervisor（finance-ledger/finance，ceiling=restricted）→ 含 restricted。"""
    ctx = AccessContext.from_user(iam, "finance_supervisor")
    filt = build_retrieval_filter(ctx)
    assert filt.allowed_classifications == ("public", "internal", "restricted")
    assert filt.project_ids == ("finance-ledger",)
    assert filt.business_domains == ("finance",)
    assert filt.status == "active"


def test_allowed_classifications_are_ascending_and_bounded(iam):
    """任意用户：分类列表为规范升序（public→internal→restricted）且不越界。"""
    canonical = ("public", "internal", "restricted")
    for uid in ("employee", "accountant", "finance_supervisor"):
        ctx = AccessContext.from_user(iam, uid)
        filt = build_retrieval_filter(ctx)
        assert filt.allowed_classifications == tuple(
            c for c in canonical if c in filt.allowed_classifications
        )
        assert all(c in canonical for c in filt.allowed_classifications)


def test_wildcard_project_yields_no_constraint(iam):
    """project='*' → project_ids=None（不限制），而不是空元组。"""
    iam.create_user("U020")
    iam.add_user_scope("U020", "project", "*")
    iam.add_user_scope("U020", "classification", "internal")
    filt = build_retrieval_filter(AccessContext.from_user(iam, "U020"))
    assert filt.project_ids is None


def test_no_scopes_is_least_privilege(iam):
    """无任何范围 → 仅 public 分类、项目/业务域不限制（分类 ceiling 兜底）。"""
    iam.create_user("U021")
    filt = build_retrieval_filter(AccessContext.from_user(iam, "U021"))
    assert filt.allowed_classifications == ("public",)
    assert filt.project_ids is None
    assert filt.business_domains is None


def test_filter_is_frozen(iam):
    ctx = AccessContext.from_user(iam, "accountant")
    filt = build_retrieval_filter(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        filt.allowed_classifications = ("restricted",)  # type: ignore[misc]


def test_filter_param_dict(iam):
    """to_param_dict() 返回纯字典，供集成方绑定参数（非 SQL 文本）。"""
    ctx = AccessContext.from_user(iam, "accountant")
    params = build_retrieval_filter(ctx).to_param_dict()
    assert params == {
        "classifications": ("public", "internal"),
        "project_ids": ("finance-ledger",),
        "business_domains": ("finance",),
        "status": "active",
    }


def test_filter_values_can_roundtrip_through_sqlite_bindings(iam):
    """契约值可安全作为 sqlite3 绑定参数使用（无字符串拼接）。"""
    import sqlite3

    ctx = AccessContext.from_user(iam, "accountant")
    filt = build_retrieval_filter(ctx)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE docs(classification TEXT, project_id TEXT, business_domain TEXT, status TEXT)")
        conn.executemany(
            "INSERT INTO docs VALUES(?,?,?,?)",
            [
                ("public", "finance-ledger", "finance", "active"),
                ("internal", "finance-ledger", "finance", "active"),
                ("restricted", "finance-ledger", "finance", "active"),
                ("internal", "other-proj", "finance", "active"),
                ("internal", "finance-ledger", "hr", "active"),
                ("internal", "finance-ledger", "finance", "archived"),
            ],
        )
        clauses = [
            "classification IN ({})".format(",".join("?" * len(filt.allowed_classifications)))
        ]
        params: list[object] = list(filt.allowed_classifications)
        clauses.append("status = ?")
        params.append(filt.status)
        if filt.project_ids is not None:
            clauses.append("project_id IN ({})".format(",".join("?" * len(filt.project_ids))))
            params.extend(filt.project_ids)
        if filt.business_domains is not None:
            clauses.append("business_domain IN ({})".format(",".join("?" * len(filt.business_domains))))
            params.extend(filt.business_domains)
        sql = (
            "SELECT classification, project_id, business_domain, status FROM docs WHERE "
            + " AND ".join(clauses)
        )
        rows = conn.execute(sql, params).fetchall()
        # 只命中 public/internal + finance-ledger + finance + active
        assert sorted(r[0] for r in rows) == ["internal", "public"]
    finally:
        conn.close()


# ---- 文档访问谓词（fail-closed + 全局公共逃逸口）----

_SAMPLE_DOCS = [
    # (classification, project_id, business_domain, status, document_id)
    ("public", None, None, "active", "pub-untagged"),        # 全局公共 → 所有人可读
    ("internal", "finance-ledger", "finance", "active", "int-fin"),
    ("internal", "other-proj", "finance", "active", "int-other-proj"),
    ("internal", "finance-ledger", "hr", "active", "int-hr"),
    ("internal", None, None, "active", "int-untagged"),      # 未打标签 → 受限用户拒绝
    ("restricted", "finance-ledger", "finance", "active", "res-fin"),
    ("internal", "finance-ledger", "finance", "archived", "arch"),
]


def _query_docs(filt, prefix=""):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE docs (document_id TEXT, classification TEXT, "
            "project_id TEXT, business_domain TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO docs VALUES(?,?,?,?,?)",
            [(d[4], d[0], d[1], d[2], d[3]) for d in _SAMPLE_DOCS],
        )
        clause, params = build_document_access_predicate(filt, prefix=prefix)
        sql = f"SELECT document_id FROM docs WHERE {clause}"
        rows = conn.execute(sql, params).fetchall()
        return sorted(r[0] for r in rows)
    finally:
        conn.close()


def _decided_docs(iam, user_id):
    """用逐条 decide() 作为基准，与谓词过滤结果做一致性对照。"""
    ctx = AccessContext.from_user(iam, user_id)
    out = []
    for classification, project_id, business_domain, status, document_id in _SAMPLE_DOCS:
        d = decide(
            ctx,
            Action.INTERNAL_RETRIEVAL_ASK,
            ResourceMetadata(classification=classification, project_id=project_id,
                             business_domain=business_domain, status=status),
        )
        if d.allowed:
            out.append(document_id)
    return sorted(out)


def test_predicate_matches_decide_for_employee(iam):
    """employee（仅 public）：只能看到公共未绑定资源。"""
    filt = build_retrieval_filter(AccessContext.from_user(iam, "employee"))
    assert _query_docs(filt) == _decided_docs(iam, "employee")
    assert _query_docs(filt) == ["pub-untagged"]


def test_predicate_matches_decide_for_accountant(iam):
    """accountant（受限 finance-ledger/finance + internal ceiling）："""
    filt = build_retrieval_filter(AccessContext.from_user(iam, "accountant"))
    assert _query_docs(filt) == _decided_docs(iam, "accountant")
    assert _query_docs(filt) == ["int-fin", "pub-untagged"]


def test_predicate_matches_decide_for_supervisor(iam):
    """finance_supervisor（finance-ledger/finance + restricted ceiling）："""
    filt = build_retrieval_filter(AccessContext.from_user(iam, "finance_supervisor"))
    assert _query_docs(filt) == _decided_docs(iam, "finance_supervisor")
    assert _query_docs(filt) == ["int-fin", "pub-untagged", "res-fin"]


def test_predicate_uses_only_placeholders(iam):
    """谓词绝不把数据值插值进 SQL 文本：占位符数量与参数数量一致，且无字面值。"""
    filt = build_retrieval_filter(AccessContext.from_user(iam, "accountant"))
    clause, params = build_document_access_predicate(filt)
    assert clause.count("?") == len(params)
    assert "finance-ledger" not in clause
    assert "finance" not in clause
    assert "active" not in clause
    # 逃逸口与受限维度 AND 组合结构存在
    assert "project_id IS NULL" in clause
    assert "project_id IN (" in clause
    assert "business_domain IN (" in clause


def test_predicate_prefix_disambiguates_columns(iam):
    """prefix 用于限定列名，避免 JOIN 中列歧义。"""
    filt = build_retrieval_filter(AccessContext.from_user(iam, "accountant"))
    clause, _ = build_document_access_predicate(filt, prefix="d.")
    assert "d.status" in clause
    assert "d.classification" in clause
    assert "d.project_id" in clause
    assert "d.business_domain" in clause
