"""问答 → 黄金证据 检索评测测试（离线复现，仅标准库）。

原依赖 `data/knowledge_demo`（已被用户删除）。本测试改为在 setUpModule
中用临时目录自建同构演示语料（5 份文档含 1 份归档 + 8 条评测问题），
不重建 knowledge_demo，不跳过任何有意义断言。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.corpus import load_corpus, load_qa
from knowledge.eval import evaluate_retrieval, hit_at_k
from knowledge.store import KnowledgeStore

_TMP = tempfile.TemporaryDirectory(prefix="knowledge-eval-")
ROOT = Path(_TMP.name)

_DOCS = {
    "SOP-001": ("采购流程", "active",
                "# 采购流程\n标准采购流程包括需求申请、供应商评审、采购审批和订单签发。\n"
                "采购金额超过 10 万元时需要总经理审批。"),
    "SAP-101": ("SAP 系统操作指南", "active",
                "# SAP 系统\nSAP 项目 P001 的采购金额在 2026 年预算中为 120 万元。\n"
                "供应商主数据在 SAP 系统中维护。"),
    "CON-007": ("合同模板", "active",
                "# 合同模板\n标准合同包含付款条款、违约责任和保密协议。\n"
                "合同签订前需经法务审核。"),
    "HR-005": ("人事制度", "active",
               "# 人事制度\n员工请假需提前 3 个工作日申请。\n新员工入职培训为期一周。"),
    "POL-011": ("旧密码策略", "archived",
                "# 密码策略\n旧系统使用 6 位数字密码，仅要求每 90 天更换一次。"),
}

_QUESTIONS = [
    {"question": "SOP-001 的采购流程是什么？",
     "gold": {"document_id": "SOP-001"}},
    {"question": "SAP 项目 P001 的 2026 年采购金额是多少？",
     "gold": {"document_id": "SAP-101", "contains": "120"}},
    {"question": "标准合同包含哪些条款？",
     "gold": {"document_id": "CON-007"}},
    {"question": "员工请假需要提前几天申请？",
     "gold": {"document_id": "HR-005"}},
    {"question": "采购金额超过多少需要总经理审批？",
     "gold": {"document_id": "SOP-001", "contains": "10 万元"}},
    {"question": "SAP 系统中维护什么主数据？",
     "gold": {"document_id": "SAP-101"}},
    {"question": "新员工入职培训为期多久？",
     "gold": {"document_id": "HR-005"}},
    {"question": "旧系统使用几位数字密码？",  # 归档陷阱题：默认检索不应返回它
     "gold": {"document_id": "POL-011", "archived": True}},
]


def _write_doc(doc_id: str, title: str, status: str, content: str) -> None:
    d = ROOT / "docs" / doc_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {"document_id": doc_id, "title": title, "version": "1.0", "status": status}
    (d / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False),
                                     encoding="utf-8")
    (d / "content.md").write_text(content, encoding="utf-8")


def setUpModule():
    for doc_id, (title, status, content) in _DOCS.items():
        _write_doc(doc_id, title, status, content)
    (ROOT / "eval_questions.json").write_text(
        json.dumps(_QUESTIONS, ensure_ascii=False), encoding="utf-8")


def tearDownModule():
    _TMP.cleanup()


def _build_store():
    store = KnowledgeStore()
    store.ingest_many(load_corpus(ROOT / "docs"))
    return store


class TestCorpus(unittest.TestCase):
    def test_loads_five_docs_with_one_archived(self):
        docs = load_corpus(ROOT / "docs")
        ids = {d.document_id: d.status for d in docs}
        self.assertEqual(set(ids), {"SOP-001", "SAP-101", "CON-007", "HR-005", "POL-011"})
        self.assertEqual(ids["POL-011"], "archived")
        self.assertTrue(all(d.status in ("active", "archived") for d in docs))

    def test_qa_data_has_gold(self):
        qa = load_qa(ROOT / "eval_questions.json")
        self.assertGreaterEqual(len(qa), 5)
        for q in qa:
            self.assertIn("question", q)
            self.assertIn("gold", q)
            self.assertIn("document_id", q["gold"])


class TestEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = _build_store()
        cls.qa = load_qa(ROOT / "eval_questions.json")

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_active_questions_hit_at_5(self):
        report = evaluate_retrieval(self.store, self.qa, top_k=5)
        # 除归档"陷阱题"外的 7 题都应命中
        active = [s for s in report["details"] if not s["gold_archived"]]
        self.assertEqual(len(active), 7)
        self.assertTrue(all(s["hit@5"] for s in active), [s["question"] for s in active if not s["hit@5"]])
        self.assertEqual(report["hit@5"], 7 / 8)

    def test_archived_doc_excluded_by_default(self):
        report = evaluate_retrieval(self.store, self.qa, top_k=5)
        self.assertEqual(report["archived_exclusion_rate"], 1.0)
        trap = [s for s in report["details"] if s["gold_archived"]][0]
        self.assertTrue(trap["archived_excluded"])

    def test_mrr_positive(self):
        report = evaluate_retrieval(self.store, self.qa, top_k=5)
        self.assertGreater(report["mrr"], 0.0)

    def test_hit_at_k_helper(self):
        from knowledge.models import SearchResult
        ok = SearchResult(chunk_id="c", text="SAP-101 文档里写 12 万元。",
                          heading_path="", page=1, document_id="SAP-101",
                          title="", version="2.0", status="active")
        bad = SearchResult(chunk_id="c2", text="别的文档。", heading_path="",
                           page=1, document_id="HR-005", title="",
                           version="1.0", status="active")
        self.assertTrue(hit_at_k([bad, ok], {"document_id": "SAP-101", "contains": "12 万元"}))
        self.assertFalse(hit_at_k([bad], {"document_id": "SAP-101", "contains": "12 万元"}))


if __name__ == "__main__":
    unittest.main()
