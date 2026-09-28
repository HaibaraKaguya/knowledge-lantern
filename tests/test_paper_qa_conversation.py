"""ConversationStore 测试 — 服务端会话 + 问答轮次持久化 / 归属隔离 / 向后兼容。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from paper_qa.conversation import (  # noqa: E402
    ConversationNotFoundError,
    ConversationOwnershipError,
    ConversationStore,
)


class TestConversationStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = ConversationStore(self.tmp.name)

    def tearDown(self):
        try:
            Path(self.tmp.name).unlink()
        except FileNotFoundError:
            pass

    def _add_turn(self, **kw):
        sources = kw.pop("sources_json", "[]")
        defaults = dict(
            session_id="", question="q", answer="a", sources_json=sources,
            model_status="available", route="INTERNAL_RETRIEVAL",
        )
        defaults.update(kw)
        return self.store.add_turn(**defaults)

    def test_create_conversation_and_roundtrip(self):
        meta = self.store.create_conversation("accountant")
        self.assertIn("conversation_id", meta)
        self.assertEqual(meta["owner_user_id"], "accountant")
        got = self.store.get_conversation(meta["conversation_id"], "accountant")
        self.assertEqual(got["conversation_id"], meta["conversation_id"])

    def test_cross_user_access_denied(self):
        conv = self.store.create_conversation("accountant")
        with self.assertRaises(ConversationOwnershipError):
            self.store.get_conversation(conv["conversation_id"], "finance_supervisor")
        with self.assertRaises(ConversationOwnershipError):
            self.store.get_conversation(conv["conversation_id"], "employee")

    def test_missing_conversation_not_found(self):
        with self.assertRaises(ConversationNotFoundError):
            self.store.get_conversation("nope", "accountant")

    def test_list_conversations_owned_only(self):
        a = self.store.create_conversation("accountant")
        s = self.store.create_conversation("finance_supervisor")
        self.store.create_conversation("accountant")
        acc = {c["conversation_id"] for c in self.store.list_conversations("accountant")}
        self.assertIn(a["conversation_id"], acc)
        self.assertNotIn(s["conversation_id"], acc)

    def test_add_turn_persists_all_fields(self):
        conv = self.store.create_conversation("accountant")
        sources = json.dumps([{"source": 1, "chunk_id": "FC-0001:1:1"}], ensure_ascii=False)
        meta = self.store.add_turn(
            conversation_id=conv["conversation_id"], owner_user_id="accountant",
            question="月度结账？", answer="见 [1]。", sources_json=sources,
            model_status="available", route="INTERNAL_RETRIEVAL",
        )
        self.assertIn("created_at", meta)
        conn = sqlite3.connect(self.tmp.name)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paper_qa_turns WHERE turn_id = ?", (meta["turn_id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(row["conversation_id"], conv["conversation_id"])
        self.assertEqual(row["owner_user_id"], "accountant")
        self.assertEqual(row["question"], "月度结账？")
        self.assertEqual(row["route"], "INTERNAL_RETRIEVAL")
        self.assertEqual(json.loads(row["sources_json"])[0]["chunk_id"], "FC-0001:1:1")

    def test_add_turn_to_foreign_conversation_denied(self):
        conv = self.store.create_conversation("accountant")
        with self.assertRaises(ConversationOwnershipError):
            self._add_turn(conversation_id=conv["conversation_id"],
                           owner_user_id="finance_supervisor")

    def test_anonymous_legacy_turn_still_supported(self):
        meta = self.store.add_turn(
            session_id="legacy-session", question="q", answer="a", sources_json="[]"
        )
        self.assertIsNotNone(meta["turn_id"])
        self.assertEqual(self.store.count_turns(session_id="legacy-session"), 1)

    def test_no_corpus_tables_created(self):
        self._add_turn(session_id="s1")
        conn = sqlite3.connect(self.tmp.name)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        self.assertIn("paper_qa_turns", tables)
        for forbidden in ("documents", "chunks", "relations", "chunks_fts"):
            self.assertNotIn(forbidden, tables, "不得在会话库创建语料表")

    def test_count_turns_total_and_per_conversation(self):
        c1 = self.store.create_conversation("accountant")
        self.store.create_conversation("employee")
        self._add_turn(conversation_id=c1["conversation_id"], owner_user_id="accountant")
        self._add_turn(conversation_id=c1["conversation_id"], owner_user_id="accountant")
        self.assertEqual(self.store.count_turns(), 2)
        self.assertEqual(
            self.store.count_turns(conversation_id=c1["conversation_id"],
                                   owner_user_id="accountant"),
            2,
        )

    def test_get_conversation_turns_ordered(self):
        conv = self.store.create_conversation("accountant")
        self._add_turn(conversation_id=conv["conversation_id"], owner_user_id="accountant",
                       question="q1")
        self._add_turn(conversation_id=conv["conversation_id"], owner_user_id="accountant",
                       question="q2")
        turns = self.store.get_conversation_turns(conv["conversation_id"], "accountant")
        self.assertEqual([t["question"] for t in turns], ["q1", "q2"])
        self.assertIn("sources", turns[0])

    def test_empty_db_creates_table_lazily(self):
        fresh = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fresh.close()
        try:
            s = ConversationStore(fresh.name)
            self.assertEqual(s.count_turns(), 0)
            s.add_turn(session_id="x", question="q", answer="a", sources_json="[]")
            self.assertEqual(s.count_turns(), 1)
        finally:
            Path(fresh.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
