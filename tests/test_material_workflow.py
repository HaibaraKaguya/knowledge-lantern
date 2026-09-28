"""资料工作台服务端校验测试（material 模块，真实组件，无 HTTP）。

覆盖服务端强制规则：
  - 角色动作：employee/supervisor 无提交权，accountant 无审核权；
  - 分类 ceiling：accountant 不能提交 restricted；
  - 元数据映射：internal/restricted → finance-ledger/finance，public → 项目不绑定；
  - 审核：不能审核自己提交的资料（SELF_REVIEW_DENIED）、非 pending 拒审、
    文档不存在 404 语义；
  - 审核人恒为 user_id（接口结构上不存在可伪造的 reviewer 参数）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from access.iam import IAMStore  # noqa: E402
from access.seed import initialize_dev_iam  # noqa: E402
from knowledge.store import KnowledgeStore  # noqa: E402
from paper_qa.material import (  # noqa: E402
    MaterialError,
    review_material,
    submit_material,
)


class MaterialWorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.iam_path = str(Path(self.tmp.name) / "iam.db")
        self.kb_path = str(Path(self.tmp.name) / "knowledge.db")
        initialize_dev_iam(self.iam_path, force=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, user_id, **kw):
        base = dict(title="测试资料", content="演示正文内容。",
                    classification="internal")
        base.update(kw)
        return submit_material(self.kb_path, self.iam_path, user_id=user_id, **base)

    def _review(self, document_id, user_id, action="publish", note=""):
        return review_material(self.kb_path, self.iam_path,
                               document_id=document_id, user_id=user_id,
                               action=action, note=note)

    def _doc(self, document_id):
        store = KnowledgeStore(self.kb_path)
        try:
            row = store.conn.execute(
                "SELECT * FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            store.close()

    # ── 角色动作 ──

    def test_employee_cannot_submit(self):
        with self.assertRaises(MaterialError) as cm:
            self._submit("employee")
        self.assertEqual(cm.exception.reason, "ACTION_NOT_PERMITTED")

    def test_supervisor_cannot_submit(self):
        with self.assertRaises(MaterialError) as cm:
            self._submit("finance_supervisor")
        self.assertEqual(cm.exception.reason, "ACTION_NOT_PERMITTED")

    def test_accountant_cannot_review(self):
        doc = self._submit("accountant", document_id="M-1")
        with self.assertRaises(MaterialError) as cm:
            self._review(doc["document_id"], "accountant")
        self.assertEqual(cm.exception.reason, "ACTION_NOT_PERMITTED")

    # ── 分类 ceiling 与元数据映射 ──

    def test_accountant_cannot_submit_restricted(self):
        with self.assertRaises(MaterialError) as cm:
            self._submit("accountant", classification="restricted")
        self.assertEqual(cm.exception.reason, "CLASSIFICATION_EXCEEDS_CEILING")

    def test_public_submission_is_project_unbound(self):
        doc = self._submit("accountant", document_id="M-PUB",
                           classification="public")
        self.assertEqual(doc["status"], "pending_review")
        row = self._doc("M-PUB")
        self.assertEqual(row["classification"], "public")
        self.assertIsNone(row["project_id"])

    def test_internal_submission_maps_to_finance_scope(self):
        doc = self._submit("accountant", document_id="M-INT",
                           classification="internal")
        row = self._doc(doc["document_id"])
        self.assertEqual(row["classification"], "internal")
        self.assertEqual(row["project_id"], "finance-ledger")
        self.assertEqual(row["business_domain"], "finance")
        self.assertEqual(row["submitter_id"], "accountant")
        self.assertEqual(row["status"], "pending_review")

    def test_invalid_inputs(self):
        with self.assertRaises(MaterialError) as cm:
            self._submit("accountant", classification="top-secret")
        self.assertEqual(cm.exception.reason, "INVALID_CLASSIFICATION")
        with self.assertRaises(MaterialError) as cm:
            self._submit("accountant", department="不存在部门")
        self.assertEqual(cm.exception.reason, "INVALID_DEPARTMENT")
        with self.assertRaises(MaterialError) as cm:
            self._submit("accountant", content="   ")
        self.assertEqual(cm.exception.reason, "EMPTY_CONTENT")

    # ── 审核（主管）──

    def test_supervisor_publish_makes_active(self):
        doc = self._submit("accountant", document_id="M-OK")
        res = self._review(doc["document_id"], "finance_supervisor",
                           action="publish", note="同意")
        self.assertEqual(res["status"], "active")
        self.assertEqual(res["reviewer_id"], "finance_supervisor")
        row = self._doc(doc["document_id"])
        self.assertEqual(row["reviewer_id"], "finance_supervisor")
        self.assertEqual(row["approval_note"], "同意")

    def test_supervisor_reject_marks_rejected(self):
        doc = self._submit("accountant", document_id="M-REJ")
        res = self._review(doc["document_id"], "finance_supervisor",
                           action="reject", note="口径不符")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(self._doc(doc["document_id"])["status"], "rejected")

    def test_non_pending_document_cannot_be_reviewed(self):
        doc = self._submit("accountant", document_id="M-DONE")
        self._review(doc["document_id"], "finance_supervisor", action="publish")
        with self.assertRaises(MaterialError) as cm:
            self._review(doc["document_id"], "finance_supervisor", action="publish")
        self.assertEqual(cm.exception.reason, "NOT_PENDING_REVIEW")

    def test_unknown_document_raises_not_found(self):
        with self.assertRaises(MaterialError) as cm:
            self._review("UNKNOWN", "finance_supervisor")
        self.assertEqual(cm.exception.reason, "DOCUMENT_NOT_FOUND")

    def test_invalid_review_action(self):
        doc = self._submit("accountant", document_id="M-A")
        with self.assertRaises(MaterialError) as cm:
            self._review(doc["document_id"], "finance_supervisor", action="burn")
        self.assertEqual(cm.exception.reason, "INVALID_REVIEW_ACTION")

    # ── 不能审核自己的资料（双动作用户场景）──

    def test_self_review_denied_when_user_has_both_actions(self):
        store = IAMStore(self.iam_path)
        store.create_user("U-both", user_name="双权限用户")
        store.add_user_role("U-both", "accountant")
        store.add_user_role("U-both", "finance_supervisor")
        for stype, svalue in (("project", "finance-ledger"),
                              ("business_domain", "finance"),
                              ("classification", "restricted")):
            store.add_user_scope("U-both", stype, svalue)
        store.close()
        doc = self._submit("U-both", document_id="M-SELF", classification="internal")
        with self.assertRaises(MaterialError) as cm:
            self._review(doc["document_id"], "U-both", action="publish")
        self.assertEqual(cm.exception.reason, "SELF_REVIEW_DENIED")


if __name__ == "__main__":
    unittest.main()
