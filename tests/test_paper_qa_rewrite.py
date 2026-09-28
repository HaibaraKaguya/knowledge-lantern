"""QueryRewriter 测试 — 确定性查询清洗（不调用 LLM，纯字符串处理）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_qa.rewrite import QueryRewriter  # noqa: E402


class TestQueryRewriter(unittest.TestCase):
    def setUp(self):
        self.r = QueryRewriter()

    def test_strips_leading_greetings_and_fillers(self):
        for q, expected in [
            ("你好，请问费用报销流程是什么？", "费用报销流程是什么"),
            ("请问预算调整怎么走？", "预算调整怎么走"),
            ("麻烦帮我查一下月度结账步骤", "月度结账步骤"),
            ("我想知道报销上限是多少", "报销上限是多少"),
            ("解释一下红冲处理规则？", "红冲处理规则"),
        ]:
            self.assertEqual(self.r.rewrite(q), expected, q)

    def test_whitespace_collapsed(self):
        self.assertEqual(self.r.rewrite("费用  报销\n流程"), "费用 报销 流程")

    def test_trailing_punctuation_trimmed(self):
        self.assertEqual(self.r.rewrite("年度预算怎么编？"), "年度预算怎么编")
        self.assertEqual(self.r.rewrite("年度预算怎么编。"), "年度预算怎么编")
        self.assertEqual(self.r.rewrite("年度预算怎么编！！！"), "年度预算怎么编")

    def test_blank_returns_blank(self):
        self.assertEqual(self.r.rewrite("   "), "")
        self.assertEqual(self.r.rewrite(None), "")

    def test_question_text_kept_for_fts(self):
        out = self.r.rewrite("财务合规系统的导出上限是多少")
        self.assertIn("导出上限", out)

    def test_never_blank_for_substantive_input(self):
        out = self.r.rewrite("请告诉我费用报销超支如何处理")
        self.assertNotEqual(out.strip(), "")
        self.assertIn("费用报销", out)

    def test_result_length_capped(self):
        out = self.r.rewrite("请问" + "报销流程步骤" * 30)
        self.assertLessEqual(len(out), 80)


if __name__ == "__main__":
    unittest.main()
