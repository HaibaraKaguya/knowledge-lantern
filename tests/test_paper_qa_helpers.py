"""Paper-QA 测试共享夹具 — 构建迷你知识库（真实 KnowledgeStore schema）。

语料为中文财务/部门演示条目（与检索词元化链路一致），覆盖授权差异：
  - D-PUB：public（project_id=None）——全局公共，任何角色可检索；
  - D-INT：internal + finance-ledger/finance —— 会计/主管可检索；
  - D-RES：restricted + finance-ledger/finance —— 仅主管可检索；
  - D-HR：internal + other-proj/hr —— 会计/主管（finance 范围）不可见。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from knowledge.models import Document  # noqa: E402
from knowledge.store import KnowledgeStore  # noqa: E402

FIXTURE_DOCS: tuple[dict, ...] = (
    {
        "document_id": "D-PUB",
        "title": "公司出差审批公共流程",
        "classification": "public",
        "project_id": None,
        "business_domain": "administration",
        "content": (
            "# 出差审批\n员工出差前需在 OA 系统提交出差申请，填写事由、目的地与"
            "预计天数，由直属主管审批。返回后一周内完成差旅报销，附票据与行程单。"
        ),
    },
    {
        "document_id": "D-INT",
        "title": "财务费用报销内部口径",
        "classification": "internal",
        "project_id": "finance-ledger",
        "business_domain": "finance",
        "content": (
            "# 费用报销\n财务内部口径：费用报销单需挂接预算项目，超支需专项说明；"
            "单笔超过 5000 元需财务主管复核；报销凭证保存期限为 10 年。"
        ),
    },
    {
        "document_id": "D-RES",
        "title": "财务年度预算编制口径",
        "classification": "restricted",
        "project_id": "finance-ledger",
        "business_domain": "finance",
        "content": (
            "# 年度预算\n演示受限口径：年度预算由财务主管汇总编制，事业部负责人签署；"
            "预算调整比例超过 10% 需总经理办公会审议。"
        ),
    },
    {
        "document_id": "D-HR",
        "title": "人力资源内部考勤规则",
        "classification": "internal",
        "project_id": "other-proj",
        "business_domain": "hr",
        "content": (
            "# 考勤规则\n内部考勤口径：弹性工时 9:00-18:00，月度迟到三次以上触发绩效提醒；"
            "异地打卡需提前备案。"
        ),
    },
)


def build_knowledge_db(path: str | Path) -> int:
    """构建 4 篇中文演示条目的迷你 knowledge.db（真实 schema），返回 chunk 总数。"""
    store = KnowledgeStore(path)
    try:
        total = 0
        for doc in FIXTURE_DOCS:
            total += store.ingest(Document(
                document_id=doc["document_id"],
                title=doc["title"],
                version="1.0",
                status="active",
                classification=doc["classification"],
                project_id=doc["project_id"],
                business_domain=doc["business_domain"],
                department="财务合规",
                content=doc["content"],
                metadata={"demo": True},
            ))
        return total
    finally:
        store.close()


def insert_archived_doc(path: str | Path) -> None:
    """追加一篇归档文档（默认检索必须排除）。"""
    store = KnowledgeStore(path)
    try:
        store.ingest(Document(
            document_id="D-ARCH",
            title="旧版审批规则",
            version="0.9",
            status="archived",
            classification="public",
            project_id=None,
            business_domain="administration",
            content="# 旧版\n旧审批规则要求纸面单审批，已废止。",
        ))
    finally:
        store.close()
