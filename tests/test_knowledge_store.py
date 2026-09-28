"""知识检索库测试 — 元数据校验 / 结构切分 / SQLite 精确+FTS 检索 / 归档排除 / 出处."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.chunking import chunk_document
from knowledge.metadata import MetadataError, load_document_from_files, load_metadata
from knowledge.models import Document, Entity, Relation
from knowledge.store import KnowledgeStore

CJK = "生产事故发生后现场责任人必须在 4 小时内完成首次上报。"


def make_doc(**kw) -> Document:
    base = dict(
        document_id="D-001", title="测试文档", version="1.0", status="active",
        content="# 概述\n生产事故发生后必须在 4 小时内完成首次上报。\n\n"
                "--- page 2 ---\n# 附录\nSAP 项目 P001 采购金额为 12 万元。",
    )
    base.update(kw)
    return Document(**base)


class TestMetadataValidation(unittest.TestCase):
    def test_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "metadata.json"
            p.write_text(json.dumps({"document_id": "X"}), encoding="utf-8")
            with self.assertRaises(MetadataError):
                load_metadata(p)

    def test_invalid_status_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "metadata.json"
            p.write_text(json.dumps({"document_id": "X", "title": "t",
                                     "version": "1", "status": "deleted"}),
                         encoding="utf-8")
            with self.assertRaises(MetadataError):
                load_metadata(p)

    def test_valid_metadata_ok(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "metadata.json").write_text(json.dumps(
                {"document_id": "X", "title": "t", "version": "1", "status": "active"}),
                encoding="utf-8")
            (d / "content.md").write_text("# 标题\n正文。", encoding="utf-8")
            doc = load_document_from_files(d)
        self.assertEqual(doc.document_id, "X")
        self.assertEqual(doc.status, "active")


class TestChunking(unittest.TestCase):
    def test_page_and_heading_structure(self):
        doc = make_doc()
        chunks = chunk_document(doc)
        self.assertEqual(len(chunks), 2)
        c1, c2 = chunks
        self.assertEqual((c1.page, c2.page), (1, 2))
        self.assertEqual(c1.heading_path, "# 概述")
        self.assertEqual(c2.heading_path, "# 附录")
        self.assertIn("4 小时", c1.text)
        self.assertIn("P001", c2.text)

    def test_chunk_provenance(self):
        doc = make_doc(version="2.3", status="draft")
        c = chunk_document(doc)[0]
        self.assertEqual((c.document_id, c.version, c.status), ("D-001", "2.3", "draft"))

    def test_no_page_marker_whole_page_one(self):
        doc = make_doc(content="# 标题\n一段话。")
        chunks = chunk_document(doc)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].page, 1)

    def test_max_chars_split(self):
        doc = make_doc(content="# 标题\n" + "甲乙丙丁。" * 40)
        chunks = chunk_document(doc, max_chars=60)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.text), 80)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = KnowledgeStore()

    def tearDown(self):
        self.store.close()

    def test_ingest_and_exact_search(self):
        self.store.ingest(make_doc())
        res = self.store.exact_search("首次上报")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].document_id, "D-001")
        self.assertEqual(res[0].match_type, "exact")

    def test_exact_search_matches_heading(self):
        self.store.ingest(make_doc())
        res = self.store.exact_search("概述")
        self.assertEqual(len(res), 1)

    def test_fts_search(self):
        self.store.ingest(make_doc())
        res = self.store.fts_search("事故 上报")
        self.assertTrue(any("事故" in r.text for r in res))

    def test_retrieve_ranks_overlap(self):
        self.store.ingest_many([
            make_doc(document_id="A"),
            make_doc(document_id="B", content="# 无关\n今天天气不错，适合出行。"),
        ])
        res = self.store.retrieve("生产事故上报时限", top_k=2)
        self.assertEqual(res[0].document_id, "A", "词元重叠多的文档应排前")

    def test_retrieve_excludes_archived_by_default(self):
        self.store.ingest(make_doc(document_id="ARCH", status="archived"))
        self.store.ingest(make_doc(document_id="ACT"))
        res = self.store.retrieve("生产事故上报", top_k=10)
        self.assertTrue(all(r.document_id != "ARCH" for r in res))

    def test_retrieve_includes_archived_when_requested(self):
        self.store.ingest(make_doc(document_id="ARCH", status="archived"))
        res = self.store.retrieve("生产事故上报", top_k=10, include_archived=True)
        self.assertTrue(any(r.document_id == "ARCH" for r in res))

    def test_get_document_hides_archived(self):
        self.store.ingest(make_doc(document_id="ARCH", status="archived"))
        self.assertIsNone(self.store.get_document("ARCH"))
        self.assertIsNotNone(self.store.get_document("ARCH", include_archived=True))

    def test_provenance_fields(self):
        self.store.ingest(make_doc(version="3.0"))
        r = self.store.retrieve("生产事故上报")[0]
        self.assertEqual(r.provenance(), {
            "document_id": "D-001", "page": 1, "version": "3.0", "status": "active",
        })
        self.assertEqual(r.title, "测试文档")

    def test_entities_and_relations_evidence(self):
        doc = make_doc()
        doc.entities = [Entity(name="生产事故上报", type="流程")]
        doc.relations = [Relation(subject="生产事故上报", predicate="须在",
                                  object="4 小时内上报",
                                  evidence_page=1,
                                  evidence_quote="4 小时内完成首次上报")]
        self.store.ingest(doc)
        rels = self.store.get_relations(subject="生产事故上报")
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["object"], "4 小时内上报")
        self.assertEqual(rels[0]["evidence"]["quote"], "4 小时内完成首次上报")
        # 归档文档的关系默认不可见
        self.assertEqual(len(self.store.get_relations()), 1)

    def test_search_by_entity_page_fallback(self):
        doc = make_doc()
        doc.entities = [Entity(name="安全生产部", type="部门")]
        doc.relations = [Relation(subject="安全生产部", predicate="负责",
                                  object="事故处置", evidence_page=1)]
        self.store.ingest(doc)
        res = self.store.search_by_entity("安全生产部")
        self.assertTrue(any(r.document_id == "D-001" for r in res))

    def test_delete_document(self):
        self.store.ingest(make_doc())
        self.store.delete_document("D-001")
        self.assertEqual(self.store.retrieve("生产事故上报"), [])
        self.assertIsNone(self.store.get_document("D-001", include_archived=True))

    def test_archived_relations_excluded(self):
        arch = make_doc(document_id="ARCH", status="archived")
        arch.relations = [Relation(subject="旧版规则", predicate="是",
                                   object="6 位数字密码")]
        self.store.ingest(arch)
        self.assertEqual(self.store.get_relations(subject="旧版规则"), [])


class TestPersistence(unittest.TestCase):
    def test_file_db_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "kb.db")
            s1 = KnowledgeStore(db)
            s1.ingest(make_doc())
            s1.close()

            s2 = KnowledgeStore(db)
            res = s2.retrieve("生产事故上报")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].document_id, "D-001")
            s2.close()


if __name__ == "__main__":
    unittest.main()
