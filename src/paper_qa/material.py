"""资料工作台 — 提交（会计）→ 审核发布/拒绝（主管）的服务端强制逻辑。

三类角色的最小权限在这里被落实为可重复测试的业务规则：
- employee（普通员工）：无 KNOWLEDGE_SUBMIT / KNOWLEDGE_REVIEW，任何资料操作被拒；
- accountant（财务会计）：可提交资料（public/internal），但分类不得超过自身 ceiling，
  并且**不能**审核/发布/拒绝自己的资料（提交者即审核人 → 拒绝）；
- finance_supervisor（财务主管）：可审核并发布/拒绝他人提交的 pending 资料，
  审核人恒为用户本人（不可伪造 reviewer_id），不能提交资料。

授权判定只信任 IAM 库：user_id → AccessContext（服务端构建），
客户端声称的 role/scope JSON 一律忽略。

元数据约定（与检索授权模型一致）：
- classification=public   → project_id=None, business_domain='finance'（全局公共逃逸口，
  所有角色均可检索）；
- classification=internal/restricted → project_id='finance-ledger',
  business_domain='finance'（会计/主管的可检索范围）。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from access import (
    AccessContext,
    Action,
    Classification,
    IAMStore,
)
from knowledge.models import CLASSIFICATIONS, Document
from knowledge.store import KnowledgeStore

#: 演示知识库的五个部门（展示用元数据；授权仍由 classification/project/business_domain 决定）。
DEPARTMENTS: tuple[str, ...] = ("经营分析", "财务合规", "人力资源", "项目与运营", "行政")
DEFAULT_DEPARTMENT: str = "财务合规"

#: 内部/受限资料的授权归属（与 knowledge_demo 语料一致）。
FINANCE_PROJECT = "finance-ledger"
FINANCE_DOMAIN = "finance"


class MaterialError(Exception):
    """资料工作台领域错误。reason 为机器可读码，HTTP 层映射状态码。"""

    def __init__(self, reason: str, *, user_id: str | None = None,
                 document_id: str | None = None, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id
        self.document_id = document_id
        self.detail = detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_classification(value: str) -> str:
    if value not in CLASSIFICATIONS:
        raise MaterialError(
            "INVALID_CLASSIFICATION",
            detail=f"非法分类: {value!r}（可选: {list(CLASSIFICATIONS)}）",
        )
    return value


def _coerce_department(value: str) -> str:
    if value not in DEPARTMENTS:
        raise MaterialError(
            "INVALID_DEPARTMENT",
            detail=f"非法部门: {value!r}（可选: {list(DEPARTMENTS)}）",
        )
    return value


def _submitter_context(iam_path: str | Path, user_id: str) -> AccessContext:
    with IAMStore(iam_path) as store:
        return AccessContext.from_user(store, user_id)


def _new_document_id() -> str:
    return f"DOC-{uuid.uuid4().hex[:12].upper()}"


def submit_material(
    kb_path: str | Path,
    iam_path: str | Path,
    *,
    user_id: str,
    title: str,
    content: str,
    classification: str = "internal",
    department: str | None = None,
    document_id: str | None = None,
    version: str = "1",
    source: str = "",
) -> dict[str, Any]:
    """会计/主管提交资料进入待审核区（pending_review），不经审核不进入检索。

    服务端强制：
    1. 动作授权 —— 无 KNOWLEDGE_SUBMIT 的用户（员工/主管）→ 拒绝；
    2. 分类合法性 + 不超过提交者 ceiling（会计不能提交 restricted）；
    3. 部门合法性；
    4. content/title 非空。

    返回 {document_id, status, chunks_created, content_hash}。
    """
    ctx = _submitter_context(iam_path, user_id)
    if Action.KNOWLEDGE_SUBMIT not in ctx.allowed_actions:
        raise MaterialError("ACTION_NOT_PERMITTED", user_id=user_id,
                            detail="当前角色无资料提交权限")

    classification = _coerce_classification(classification)
    department = _coerce_department(department or DEFAULT_DEPARTMENT)
    if not title or not title.strip():
        raise MaterialError("EMPTY_TITLE", user_id=user_id, detail="title 不能为空")
    if not content or not content.strip():
        raise MaterialError("EMPTY_CONTENT", user_id=user_id, detail="content 不能为空")

    req_rank = Classification(classification).rank
    if req_rank > ctx.classification_ceiling.rank:
        raise MaterialError(
            "CLASSIFICATION_EXCEEDS_CEILING", user_id=user_id,
            detail=(
                f"提交分类 {classification} 超过当前身份上限 "
                f"{ctx.classification_ceiling.value}"
            ),
        )

    if classification == "public":
        project_id = None
    else:
        project_id = FINANCE_PROJECT
    business_domain = FINANCE_DOMAIN

    doc_id = document_id or _new_document_id()
    now = _now_iso()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    doc = Document(
        document_id=doc_id,
        title=title.strip(),
        version=version,
        status="pending_review",
        classification=classification,
        project_id=project_id,
        business_domain=business_domain,
        department=department,
        owner="",
        source=source,
        pages=1,
        created=now,
        updated=now,
        content=content,
        submitter_id=user_id,
        submitted_at=now,
        content_hash=digest,
    )

    store = KnowledgeStore(kb_path)
    try:
        chunks_created = store.ingest(doc)
    finally:
        store.close()
    return {
        "document_id": doc_id,
        "status": "pending_review",
        "chunks_created": chunks_created,
        "content_hash": digest,
    }


def _review_check(
    kb_path: str | Path,
    iam_path: str | Path,
    *,
    user_id: str,
    document_id: str,
) -> tuple[dict, str]:
    """审核公共前置校验（动作/存在性/状态/不能审自己），返回 (doc_row, reviewer)。"""
    ctx = _submitter_context(iam_path, user_id)
    if Action.KNOWLEDGE_REVIEW not in ctx.allowed_actions:
        raise MaterialError("ACTION_NOT_PERMITTED", user_id=user_id,
                            document_id=document_id,
                            detail="当前角色无资料审核权限")

    store = KnowledgeStore(kb_path)
    row = store.conn.execute(
        "SELECT submitter_id, status FROM documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if row is None:
        store.close()
        raise MaterialError("DOCUMENT_NOT_FOUND", user_id=user_id,
                            document_id=document_id, detail="文档不存在")
    submitter_id = row["submitter_id"]
    status = row["status"]

    if submitter_id == user_id:
        store.close()
        raise MaterialError("SELF_REVIEW_DENIED", user_id=user_id,
                            document_id=document_id,
                            detail="提交者不能审核自己提交的资料")
    if status != "pending_review":
        store.close()
        raise MaterialError("NOT_PENDING_REVIEW", user_id=user_id,
                            document_id=document_id,
                            detail=f"当前状态不可审核: {status}")
    return store, {"document_id": document_id, "status": status}


def review_material(
    kb_path: str | Path,
    iam_path: str | Path,
    *,
    document_id: str,
    user_id: str,
    action: str,
    note: str = "",
) -> dict[str, Any]:
    """主管审核资料：action in ('publish'|'reject')。

    审核人恒为 user_id（不可伪造）；发布/拒绝都写入 reviewer_id / reviewed_at /
    approval_note。发布后状态=active（进入检索范围），拒绝后状态=rejected。
    """
    if action not in ("publish", "reject"):
        raise MaterialError("INVALID_REVIEW_ACTION", user_id=user_id,
                            document_id=document_id,
                            detail="action 必须是 'publish' 或 'reject'")
    store, meta = _review_check(
        kb_path, iam_path, user_id=user_id, document_id=document_id,
    )
    now = _now_iso()
    try:
        if action == "publish":
            store.conn.execute(
                "UPDATE documents SET status='active', reviewer_id=?, reviewed_at=?, "
                "published_at=?, approval_note=?, updated=? WHERE document_id=?",
                (user_id, now, now, note, now, document_id),
            )
        else:
            store.conn.execute(
                "UPDATE documents SET status='rejected', reviewer_id=?, reviewed_at=?, "
                "approval_note=?, updated=? WHERE document_id=?",
                (user_id, now, note, now, document_id),
            )
        store.conn.commit()
    finally:
        store.close()
    result_status = "active" if action == "publish" else "rejected"
    return {
        "document_id": document_id,
        "status": result_status,
        "reviewer_id": user_id,
        "reviewed_at": now,
    }


def list_pending_documents(kb_path: str | Path, status: str | None = None) -> list[dict]:
    """列出资料（仅元数据，不含正文）。默认返回全部；status 过滤可选。"""
    store = KnowledgeStore(kb_path)
    try:
        sql = (
            "SELECT document_id, title, version, status, classification, project_id, "
            "business_domain, department, submitter_id, reviewer_id, submitted_at, "
            "reviewed_at, published_at, content_hash, approval_note FROM documents"
        )
        args: list[str] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY updated DESC, document_id"
        return [dict(r) for r in store.conn.execute(sql, args)]
    finally:
        store.close()
