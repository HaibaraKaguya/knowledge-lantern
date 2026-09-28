"""PaperAnswerGenerator 测试 — 假客户端注入，无网络/模型依赖。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_qa.llm import PaperAnswerGenerator, AiunavilableError, build_prompt  # noqa: E402
from paper_qa.retrieval import RetrievedChunk  # noqa: E402


class FakeClient:
    def __init__(self, content="fake answer", error=None):
        self.content = content
        self.error = error
        self.messages = None
        self.invocations = 0

    def invoke(self, messages):
        self.invocations += 1
        self.messages = messages
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(content=self.content)


def make_chunks():
    return [
        RetrievedChunk(
            chunk_id="FC-0001:1:1", document_id="FC-0001",
            title="财务合规·月末结账·月度结账",
            heading_path="# 月度结账", page=1,
            text="演示口径：结账前执行未过账单据检查，再按科目顺序过账并复核。",
            score=8.0, match_type="fts",
        )
    ]


class TestGenerate(unittest.TestCase):
    def test_generate_returns_content(self):
        fake = FakeClient(content="月度结账步骤见 [1]。")
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        out = g.generate("月度结账步骤？", make_chunks())
        self.assertEqual(out, "月度结账步骤见 [1]。")
        self.assertEqual(fake.invocations, 1)

    def test_generate_wraps_exception_as_unavailable(self):
        fake = FakeClient(error=RuntimeError("connection refused"))
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        with self.assertRaises(AiunavilableError):
            g.generate("q", make_chunks())

    def test_generate_passes_messages_to_client(self):
        fake = FakeClient()
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        g.generate("月度结账步骤？", make_chunks())
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(fake.messages[0]["role"], "system")
        self.assertIn("月度结账步骤？", fake.messages[1]["content"])
        self.assertIn("[1]", fake.messages[1]["content"])

    def test_model_info_injected(self):
        g = PaperAnswerGenerator(client=FakeClient(), model_name="qwen-x", base_url="http://u")
        self.assertEqual(g.model_info(), {"name": "qwen-x", "base_url": "http://u"})

    def test_ping_available(self):
        g = PaperAnswerGenerator(client=FakeClient(), model_name="m", base_url="u")
        self.assertEqual(g.ping()["status"], "available")
        self.assertIsNone(g.ping()["error"])

    def test_ping_unavailable(self):
        g = PaperAnswerGenerator(
            client=FakeClient(error=RuntimeError("down")), model_name="m", base_url="u"
        )
        ping = g.ping()
        self.assertEqual(ping["status"], "unavailable")
        self.assertIn("down", ping["error"])

    def test_generate_empty_content_raises_unavailable(self):
        fake = FakeClient(content="   ")
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        with self.assertRaises(AiunavilableError):
            g.generate("q", make_chunks())

    def test_generate_none_content_raises_unavailable(self):
        fake = FakeClient(content=None)
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        with self.assertRaises(AiunavilableError):
            g.generate("q", make_chunks())

    def test_ping_empty_content_unavailable(self):
        fake = FakeClient(content="")
        g = PaperAnswerGenerator(client=fake, model_name="m", base_url="u")
        ping = g.ping()
        self.assertEqual(ping["status"], "unavailable")
        self.assertIn("空内容", ping["error"])

    def test_extract_text_list_blocks(self):
        response = types.SimpleNamespace(content=[
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ])
        self.assertEqual(PaperAnswerGenerator._extract_text(response), "first\nsecond")

    def test_extract_text_raises_on_empty(self):
        from paper_qa.llm import extract_text
        with self.assertRaises(AiunavilableError):
            extract_text(types.SimpleNamespace(content="   "))
        with self.assertRaises(AiunavilableError):
            extract_text(types.SimpleNamespace(content=[]))
        with self.assertRaises(AiunavilableError):
            extract_text(types.SimpleNamespace(content=None))


class TestBuildPrompt(unittest.TestCase):
    def test_instructions_and_citations(self):
        messages = build_prompt("月度结账怎么处理？", make_chunks())
        content = messages[0]["content"] + messages[1]["content"]
        self.assertIn("[1]", content)
        self.assertIn("现有资料不足以回答", content)
        self.assertIn("引用证据编号", content)
        self.assertIn("月度结账怎么处理？", messages[1]["content"])

    def test_no_evidence_message(self):
        messages = build_prompt("q", [])
        self.assertIn("未检索到相关证据", messages[1]["content"])

    def test_same_language_instruction(self):
        messages = build_prompt("q", make_chunks())
        self.assertIn("与用户问题相同的语言", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
