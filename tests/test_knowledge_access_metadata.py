"""授权元数据迁移测试 — fresh schema / 旧 schema 迁移 / 语料回填 / 幂等 / 检索继承.

覆盖:
  1) 全新库（KnowledgeStore）schema 自带授权列与默认值
  2) 旧 schema（无授权列）迁移: 补列、既有行回填默认值、索引
  3) 当前公开语料回填: corpus 范围只改 arXiv 论文，幂等
  4) 回填 scope=all / ids / dry-run
  5) 事务保护（异常回滚）
  6) 块检索继承文档级授权元数据（chunks 无重复副本，改文档即时可见）
  7) 向后兼容: provenance() 形状不变、旧 INSERT 省略新列仍可用
  8) CLI 端到端（副本）
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.metadata import MetadataError, load_document_from_files
from knowledge.migrations import (
    AUTH_COLUMNS,
    CONTENT_CONTRACT_COLUMNS,
    KnowledgeMigrationError,
    SEED_SOURCE,
    backfill_content_contract,
    backfill_public_corpus,
    backfill_seed_provenance,
    migrate_content_contract,
    migrate_schema,
    run_migrations,
)
from knowledge.models import (
    APPLICABILITY_FIELDS,
    Document,
    LEGACY_DEPARTMENT_CONTRACT,
    PROVENANCE_FIELDS,
    SEED_LABEL,
    SEED_LICENSE,
    SEED_ORIGIN,
    SEED_PROVENANCE,
    SearchResult,
)
from knowledge.store import KnowledgeStore

OLD_SCHEMA = """
CREATE TABLE documents (
    document_id   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    version       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    department    TEXT NOT NULL DEFAULT '',
    owner         TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    pages         INTEGER NOT NULL DEFAULT 1,
    created       TEXT NOT NULL DEFAULT '',
    updated       TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def make_old_db(rows=((("arxiv-1", "P1", "2020", "https://arxiv.org/abs/1"),
                       ("doc-x", "PX", "2021", "file://internal")))):
    """建一个旧 schema 内存库并插入若干行。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO documents(document_id,title,version,source) VALUES(?,?,?,?)",
        rows,
    )
    return conn


def doc_columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(documents)")}


def doc_values(conn, document_id):
    row = conn.execute(
        "SELECT classification, project_id, business_domain, status "
        "FROM documents WHERE document_id=?", (document_id,)
    ).fetchone()
    return tuple(row)


class TestFreshSchema(unittest.TestCase):
    def test_new_store_has_auth_columns(self):
        s = KnowledgeStore()
        try:
            cols = doc_columns(s.conn)
            for col in AUTH_COLUMNS:
                self.assertIn(col, cols, f"fresh schema 应含 {col}")
        finally:
            s.close()

    def test_new_store_defaults(self):
        s = KnowledgeStore()
        try:
            s.ingest(Document(document_id="D-1", title="t", version="1"))
            self.assertEqual(
                doc_values(s.conn, "D-1"),
                ("public", None, "research", "active"),
            )
        finally:
            s.close()

    def test_indexes_present_on_fresh_schema(self):
        s = KnowledgeStore()
        try:
            names = {r[0] for r in s.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_documents_%'")}
            self.assertIn("idx_documents_active", names)
            self.assertIn("idx_documents_auth", names)
        finally:
            s.close()


class TestOldSchemaMigration(unittest.TestCase):
    def test_migrate_adds_columns_and_backfills_defaults(self):
        conn = make_old_db()
        rep = migrate_schema(conn)
        self.assertEqual(set(rep["added_columns"]), set(AUTH_COLUMNS))
        self.assertEqual(set(rep["added_indexes"]),
                         {"idx_documents_active", "idx_documents_auth"})
        # 既有行经 ADD COLUMN DEFAULT 立即获得默认授权元数据
        self.assertEqual(doc_values(conn, "arxiv-1"),
                         ("public", None, "research", "active"))
        self.assertEqual(doc_values(conn, "doc-x"),
                         ("public", None, "research", "active"))

    def test_migrate_idempotent(self):
        conn = make_old_db()
        migrate_schema(conn)
        rep2 = migrate_schema(conn)
        self.assertEqual(rep2["added_columns"], [])
        self.assertEqual(rep2["added_indexes"], [])

    def test_migrate_dry_run_does_not_write(self):
        conn = make_old_db()
        rep = migrate_schema(conn, dry_run=True)
        self.assertEqual(set(rep["added_columns"]), set(AUTH_COLUMNS))
        self.assertNotIn("classification", doc_columns(conn))

    def test_migrate_missing_documents_table_raises(self):
        conn = sqlite3.connect(":memory:")
        with self.assertRaises(KnowledgeMigrationError):
            migrate_schema(conn)


class TestBackfill(unittest.TestCase):
    def _migrated(self):
        conn = make_old_db()
        migrate_schema(conn)
        return conn

    def test_backfill_corpus_only_arxiv(self):
        conn = self._migrated()
        # 破坏 arxiv-1 的值，验证只回填语料内文档
        conn.execute("UPDATE documents SET business_domain='finance' WHERE document_id='arxiv-1'")
        conn.execute("UPDATE documents SET status='draft' WHERE document_id='doc-x'")
        rep = backfill_public_corpus(conn, scope="corpus")
        self.assertEqual(rep["total_in_scope"], 1)
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(doc_values(conn, "arxiv-1"),
                         ("public", None, "research", "active"))
        # 非 arXiv 文档不受 corpus 回填影响
        self.assertEqual(doc_values(conn, "doc-x"),
                         ("public", None, "research", "draft"))

    def test_backfill_idempotent(self):
        conn = self._migrated()
        rep1 = backfill_public_corpus(conn, scope="corpus")
        rep2 = backfill_public_corpus(conn, scope="corpus")
        self.assertEqual(rep1["updated"], rep2["updated"])
        self.assertEqual(rep2["updated"], 0)
        self.assertEqual(rep2["conforming"], rep1["total_in_scope"])

    def test_backfill_scope_all(self):
        conn = self._migrated()
        conn.execute("UPDATE documents SET status='draft'")
        rep = backfill_public_corpus(conn, scope="all")
        self.assertEqual(rep["total_in_scope"], 2)
        self.assertEqual(rep["updated"], 2)
        self.assertEqual(doc_values(conn, "doc-x"),
                         ("public", None, "research", "active"))

    def test_backfill_scope_ids(self):
        conn = self._migrated()
        conn.execute("UPDATE documents SET status='draft'")
        rep = backfill_public_corpus(conn, scope="ids", document_ids=["doc-x"])
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(doc_values(conn, "doc-x"),
                         ("public", None, "research", "active"))
        self.assertEqual(doc_values(conn, "arxiv-1")[3], "draft")

    def test_backfill_scope_ids_requires_ids(self):
        conn = self._migrated()
        with self.assertRaises(KnowledgeMigrationError):
            backfill_public_corpus(conn, scope="ids")

    def test_backfill_dry_run_no_write(self):
        conn = self._migrated()
        conn.execute("UPDATE documents SET status='draft' WHERE document_id='arxiv-1'")
        rep = backfill_public_corpus(conn, scope="corpus", dry_run=True)
        self.assertEqual(rep["updated"], 0)
        self.assertEqual(rep["conforming"], 0)
        self.assertEqual(rep["total_in_scope"], 1)
        self.assertEqual(doc_values(conn, "arxiv-1")[3], "draft")

    def test_backfill_before_migration_raises(self):
        conn = make_old_db()
        with self.assertRaises(KnowledgeMigrationError):
            backfill_public_corpus(conn, scope="corpus")


class TestTransactionSafety(unittest.TestCase):
    def test_run_migrations_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "kb.db")
            conn = sqlite3.connect(db)
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO documents(document_id,title,version) VALUES('a','t','1')")
            conn.commit()
            conn.close()
            # 非法 scope 使回填失败 → 迁移也应整体回滚
            with self.assertRaises(KnowledgeMigrationError):
                run_migrations(db, backfill=True, scope="bogus")
            conn = sqlite3.connect(db)
            self.assertNotIn("classification", doc_columns(conn))


class TestRetrievalInheritsAuth(unittest.TestCase):
    def test_retrieval_result_carries_auth_metadata(self):
        s = KnowledgeStore()
        try:
            s.ingest(Document(
                document_id="D-9", title="内部方案", version="1",
                classification="internal", project_id="proj-x",
                business_domain="finance",
                content="# 概述\n内部采购金额 500 万。",
            ))
            r = s.retrieve("内部采购金额")[0]
            self.assertEqual(r.authorization(), {
                "document_id": "D-9",
                "classification": "internal",
                "access_scope": "proj-x",
                "project_id": "proj-x",
                "business_domain": "finance",
                "status": "active",
            })
        finally:
            s.close()

    def test_auth_change_on_document_reflected_immediately(self):
        """授权元数据不复制到 chunk：改文档级策略，检索立即继承，无陈旧副本。"""
        s = KnowledgeStore()
        try:
            s.ingest(Document(document_id="D-9", title="t", version="1",
                              content="# 概述\n一些正文内容。"))
            s.conn.execute(
                "UPDATE documents SET classification='restricted', "
                "project_id='proj-sec', business_domain='hr' "
                "WHERE document_id='D-9'")
            s.conn.commit()
            r = s.retrieve("一些正文内容")[0]
            self.assertEqual(r.classification, "restricted")
            self.assertEqual(r.project_id, "proj-sec")
            self.assertEqual(r.business_domain, "hr")
        finally:
            s.close()

    def test_chunks_table_has_no_auth_columns(self):
        """不在块级重复可变策略，防止不一致副本。"""
        s = KnowledgeStore()
        try:
            cols = {c[1] for c in s.conn.execute("PRAGMA table_info(chunks)")}
            for col in AUTH_COLUMNS:
                self.assertNotIn(col, cols)
        finally:
            s.close()

    def test_provenance_unchanged(self):
        """向后兼容：provenance() 保持原形状，授权元数据走 authorization()。"""
        s = KnowledgeStore()
        try:
            s.ingest(Document(document_id="D-1", title="t", version="3",
                              content="# 概述\n一些正文内容。"))
            r = s.retrieve("概述", top_k=1)[0]
            self.assertEqual(r.provenance(), {
                "document_id": "D-1", "page": 1, "version": "3", "status": "active",
            })
        finally:
            s.close()


class TestBackwardCompatIngest(unittest.TestCase):
    def test_old_insert_without_auth_columns_still_works(self):
        """旧代码 INSERT OR REPLACE 省略新列仍可用，获取默认值。"""
        s = KnowledgeStore()
        try:
            s.conn.execute(
                "INSERT OR REPLACE INTO documents(document_id,title,version,status) "
                "VALUES(?,?,?,?)", ("D-old", "旧文档", "1", "active"))
            s.conn.commit()
            self.assertEqual(doc_values(s.conn, "D-old"),
                             ("public", None, "research", "active"))
        finally:
            s.close()


class TestIngestAndMetadataLoader(unittest.TestCase):
    def test_metadata_loader_parses_auth_fields(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "metadata.json").write_text(json.dumps({
                "document_id": "X", "title": "t", "version": "1",
                "classification": "restricted", "project_id": "p1",
                "business_domain": "hr",
            }), encoding="utf-8")
            (d / "content.md").write_text("# 标题\n正文。", encoding="utf-8")
            doc = load_document_from_files(d)
            self.assertEqual(doc.classification, "restricted")
            self.assertEqual(doc.project_id, "p1")
            self.assertEqual(doc.business_domain, "hr")

    def test_metadata_loader_rejects_bad_classification(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "metadata.json").write_text(json.dumps({
                "document_id": "X", "title": "t", "version": "1",
                "classification": "topsecret",
            }), encoding="utf-8")
            with self.assertRaises(MetadataError):
                load_document_from_files(d)

    def test_searchresult_authorization_defaults(self):
        r = SearchResult(chunk_id="c", text="t", heading_path="",
                         page=1, document_id="d", title="T", version="1",
                         status="active")
        self.assertEqual(r.authorization(), {
            "document_id": "d", "classification": "public",
            "access_scope": "company_public",
            "project_id": None, "business_domain": "research",
            "status": "active",
        })
        # 内容适用范围是独立形状，绝不混入授权形状
        self.assertEqual(r.content_applicability(), {
            "applicable_departments": [], "owner_department": "",
            "business_category": "",
        })


class TestSeedProvenanceBackfill(unittest.TestCase):
    """测试期种子知识出处回填：只增键、幂等、不动内容/授权列。"""

    def _seed_db(self):
        """建一个含 2 条种子文档（各 1 chunk）的库。"""
        s = KnowledgeStore()
        s.ingest(Document(
            document_id="SEED-1", title="种子一", version="1",
            department="财务合规", classification="internal",
            project_id="finance-ledger", business_domain="finance",
            source=SEED_SOURCE, content="# 概述\n结账窗口期需先检查未过账单据。",
            metadata={"category": "月末/年末结账", "demo": True},
        ))
        s.ingest(Document(
            document_id="SEED-2", title="种子二", version="1",
            department="行政", classification="public", source=SEED_SOURCE,
            content="# 概述\n会议室预定需提前一天提交。",
            metadata={"category": "业务流程", "demo": True},
        ))
        return s

    def test_backfill_adds_provenance_and_is_idempotent(self):
        s = self._seed_db()
        try:
            rep = backfill_seed_provenance(s.conn)
            s.conn.commit()
            self.assertEqual(rep["documents_in_scope"], 2)
            self.assertEqual(rep["documents_updated"], 2)
            self.assertEqual(rep["chunks_updated"], 2)
            for table, key in (("documents", "document_id"), ("chunks", "chunk_id")):
                for (mj,) in s.conn.execute(
                        f"SELECT metadata_json FROM {table}").fetchall():
                    meta = json.loads(mj)
                    for k, v in SEED_PROVENANCE.items():
                        self.assertEqual(meta.get(k), v, f"{table}:{key}:{k}")
            # 幂等：重复执行 0 变更
            rep2 = backfill_seed_provenance(s.conn)
            s.conn.commit()
            self.assertEqual(rep2["documents_updated"], 0)
            self.assertEqual(rep2["chunks_updated"], 0)
        finally:
            s.close()

    def test_backfill_dry_run_writes_nothing(self):
        s = self._seed_db()
        try:
            rep = backfill_seed_provenance(s.conn, dry_run=True)
            self.assertEqual(rep["documents_updated"], 2)
            (mj,) = s.conn.execute(
                "SELECT metadata_json FROM documents WHERE document_id='SEED-1'"
            ).fetchone()
            self.assertNotIn("origin", json.loads(mj))
        finally:
            s.close()

    def test_backfill_preserves_content_auth_and_ids(self):
        s = self._seed_db()
        try:
            before = s.conn.execute(
                "SELECT text, chunk_id FROM chunks ORDER BY chunk_id").fetchall()
            before_auth = doc_values(s.conn, "SEED-1")
            backfill_seed_provenance(s.conn)
            s.conn.commit()
            after = s.conn.execute(
                "SELECT text, chunk_id FROM chunks ORDER BY chunk_id").fetchall()
            self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
            self.assertEqual(doc_values(s.conn, "SEED-1"), before_auth)
        finally:
            s.close()

    def test_backfill_only_touches_seed_source(self):
        s = self._seed_db()
        try:
            s.ingest(Document(document_id="OTHER-1", title="用户资料", version="1",
                              source="资料中心工作台",
                              content="# 概述\n用户提交的内容。"))
            backfill_seed_provenance(s.conn)
            s.conn.commit()
            (mj,) = s.conn.execute(
                "SELECT metadata_json FROM documents WHERE document_id='OTHER-1'"
            ).fetchone()
            self.assertNotIn("origin", json.loads(mj))
        finally:
            s.close()

    def test_searchresult_data_provenance_shape(self):
        r = SearchResult(
            chunk_id="c", text="t", heading_path="", page=1,
            document_id="d", title="T", version="1", status="active",
            meta=dict(SEED_PROVENANCE),
        )
        self.assertEqual(r.data_provenance(), {
            "origin": SEED_ORIGIN,
            "license": SEED_LICENSE,
            "is_real_company_data": False,
            "seed_label": SEED_LABEL,
        })
        # provenance()（检索出处）形状保持不变
        self.assertEqual(r.provenance(), {
            "document_id": "d", "page": 1, "version": "1", "status": "active",
        })

    def test_finalize_meta_defaults_provenance_for_unknown_source(self):
        from knowledge.metadata import finalize_meta
        out = finalize_meta({})
        self.assertEqual(out["origin"], "unspecified")
        self.assertIsNone(out["is_real_company_data"])
        for key in PROVENANCE_FIELDS:
            self.assertIn(key, out)


class TestContentContractMigration(unittest.TestCase):
    """内容适用范围契约：列/索引表迁移 + 旧 department 标签回填 + 幂等 + 隔离授权。"""

    #: 本契约引入前的 schema（有授权列，但无适用范围列/索引表）。
    PRE_CONTRACT_SCHEMA = """
    CREATE TABLE documents (
        document_id   TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        version       TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active',
        classification TEXT NOT NULL DEFAULT 'public',
        project_id    TEXT,
        business_domain TEXT NOT NULL DEFAULT 'research',
        department    TEXT NOT NULL DEFAULT '',
        owner         TEXT NOT NULL DEFAULT '',
        source        TEXT NOT NULL DEFAULT '',
        pages         INTEGER NOT NULL DEFAULT 1,
        created       TEXT NOT NULL DEFAULT '',
        updated       TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE chunks (
        chunk_id     TEXT PRIMARY KEY,
        document_id  TEXT NOT NULL,
        page         INTEGER NOT NULL DEFAULT 1,
        version      TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'active',
        heading_path TEXT NOT NULL DEFAULT '',
        seq          INTEGER NOT NULL DEFAULT 0,
        text         TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    """

    def _legacy_db(self, *, documents=(
            # (document_id, department, classification, project_id, chunk_id)
            ("FC-1", "财务合规", "internal", "finance-ledger", "FC-1:1:1"),
            ("AD-1", "行政", "public", None, "AD-1:1:1"))):
        conn = sqlite3.connect(":memory:")
        conn.executescript(self.PRE_CONTRACT_SCHEMA)
        for doc_id, dept, cls, proj, chunk_id in documents:
            conn.execute(
                "INSERT INTO documents(document_id,title,version,classification,"
                "project_id,business_domain,department,source,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, f"{dept}文档", "1", cls, proj,
                 "finance" if cls != "public" else "research", dept,
                 SEED_SOURCE, json.dumps({"category": "系统操作"}, ensure_ascii=False)))
            conn.execute(
                "INSERT INTO chunks(chunk_id,document_id,text,metadata_json) "
                "VALUES(?,?,?,?)",
                (chunk_id, doc_id, "种子正文占位。", "{}"))
        conn.commit()
        return conn

    def test_migrate_adds_columns_and_index_table(self):
        conn = self._legacy_db()
        rep = migrate_content_contract(conn)
        self.assertEqual(set(rep["added_columns"]), set(CONTENT_CONTRACT_COLUMNS))
        self.assertTrue(rep["applicable_index_table_created"])
        cols = doc_columns(conn)
        for name in CONTENT_CONTRACT_COLUMNS:
            self.assertIn(name, cols)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("document_applicable_departments", tables)
        # 幂等
        rep2 = migrate_content_contract(conn)
        self.assertEqual(rep2["added_columns"], [])
        self.assertFalse(rep2["applicable_index_table_created"])

    def test_migrate_dry_run_does_not_write(self):
        conn = self._legacy_db()
        rep = migrate_content_contract(conn, dry_run=True)
        self.assertEqual(set(rep["added_columns"]), set(CONTENT_CONTRACT_COLUMNS))
        self.assertNotIn("owner_department", doc_columns(conn))

    def test_backfill_maps_legacy_department_and_is_idempotent(self):
        conn = self._legacy_db()
        migrate_schema(conn)
        migrate_content_contract(conn)
        rep = backfill_content_contract(conn)
        self.assertEqual(rep["documents_in_scope"], 2)
        self.assertEqual(rep["documents_updated"], 2)
        self.assertEqual(rep["chunks_in_scope"], 2)
        self.assertEqual(rep["chunks_updated"], 2)
        # 财务合规 → 业务分类 finance_compliance、owner finance、适用 [finance, business_analysis]
        row = conn.execute(
            "SELECT department, access_scope, owner_department, business_category, "
            "applicable_departments_json FROM documents WHERE document_id='FC-1'"
        ).fetchone()
        self.assertEqual(row[0], "财务合规")            # 旧标签可读且未改动
        self.assertEqual(row[1], "finance-ledger")      # 授权范围键由分类/项目派生
        self.assertEqual(row[2], "finance")
        self.assertEqual(row[3], "finance_compliance")
        self.assertEqual(json.loads(row[4]), ["finance", "business_analysis"])
        # 规范化索引表（可索引过滤）
        indexed = dict(conn.execute(
            "SELECT department_key, COUNT(*) FROM document_applicable_departments "
            "GROUP BY department_key").fetchall())
        self.assertEqual(indexed.get("finance"), 1)
        self.assertEqual(indexed.get("business_analysis"), 1)
        self.assertEqual(indexed.get("administration"), 1)
        # 幂等：重复执行 0 变更
        rep2 = backfill_content_contract(conn)
        self.assertEqual(rep2["documents_updated"], 0)
        self.assertEqual(rep2["chunks_updated"], 0)

    def test_backfill_preserves_content_auth_and_provenance(self):
        conn = self._legacy_db()
        migrate_schema(conn)
        migrate_content_contract(conn)
        doc_before = [tuple(r) for r in conn.execute(
            "SELECT document_id, department, classification, project_id, "
            "business_domain, status, metadata_json FROM documents ORDER BY document_id")]
        chunk_before = [tuple(r) for r in conn.execute(
            "SELECT chunk_id, text FROM chunks ORDER BY chunk_id")]
        backfill_content_contract(conn)
        # 授权列/内容/ID 不变
        for before, after in zip(doc_before, conn.execute(
                "SELECT document_id, department, classification, project_id, "
                "business_domain, status, metadata_json FROM documents "
                "ORDER BY document_id")):
            self.assertEqual(before[:6], tuple(after)[:6])
        self.assertEqual(chunk_before, [tuple(r) for r in conn.execute(
            "SELECT chunk_id, text FROM chunks ORDER BY chunk_id")])
        # provenance 不被伪装（未声明 origin 的 legacy 文档不会被写成种子出处）
        for (mj,) in conn.execute("SELECT metadata_json FROM documents"):
            meta = json.loads(mj)
            self.assertNotEqual(meta.get("origin"), SEED_ORIGIN)
            self.assertNotIn("is_real_company_data", meta)

    def test_backfill_dry_run_writes_nothing(self):
        conn = self._legacy_db()
        migrate_schema(conn)
        migrate_content_contract(conn)
        rep = backfill_content_contract(conn, dry_run=True)
        self.assertGreater(rep["documents_updated"], 0)
        row = conn.execute(
            "SELECT owner_department, applicable_departments_json FROM documents "
            "WHERE document_id='FC-1'").fetchone()
        self.assertEqual(row[0], "")
        self.assertEqual(row[1], "[]")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM document_applicable_departments").fetchone()[0], 0)

    def test_run_migrations_includes_content_contract(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "kb.db")
            conn = sqlite3.connect(db)
            conn.executescript(self.PRE_CONTRACT_SCHEMA)
            conn.execute(
                "INSERT INTO documents(document_id,title,version,department,source) "
                "VALUES('AD-1','行政文档','1','行政',?)", (SEED_SOURCE,))
            conn.commit()
            conn.close()
            rep = run_migrations(db, backfill=False, content_contract=True)
            self.assertIn("content_contract", rep)
            self.assertIn("content_contract_schema", rep)
            self.assertIsNotNone(rep["content_contract"])
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute(
                "SELECT business_category FROM documents WHERE document_id='AD-1'"
            ).fetchone()[0], "administration")


class TestStoreContentApplicability(unittest.TestCase):
    """store 写入/读取内容适用范围契约：多选列表 + 规范化索引 + 与授权隔离。"""

    def test_ingest_writes_and_returns_contract_fields(self):
        s = KnowledgeStore()
        try:
            s.ingest(Document(
                document_id="D-1", title="t", version="1",
                classification="internal", project_id="finance-ledger",
                department="财务合规",
                content="# 概述\n结账窗口期需先检查未过账单据。"))
            row = s.conn.execute(
                "SELECT access_scope, owner_department, business_category, "
                "applicable_departments_json FROM documents WHERE document_id='D-1'"
            ).fetchone()
            self.assertEqual(tuple(row),
                             ("finance-ledger", "finance", "finance_compliance",
                              '["finance", "business_analysis"]'))
            r = s.retrieve("结账窗口期")[0]
            self.assertEqual(r.content_applicability(), {
                "applicable_departments": ["finance", "business_analysis"],
                "owner_department": "finance",
                "business_category": "finance_compliance",
            })
            # 授权形状只含授权字段，不含适用部门
            self.assertNotIn("applicable_departments", r.authorization())
            self.assertEqual(r.authorization()["access_scope"], "finance-ledger")
        finally:
            s.close()

    def test_multi_select_list_and_indexed_lookup(self):
        s = KnowledgeStore()
        try:
            s.ingest(Document(
                document_id="D-M", title="跨部门", version="1",
                applicable_departments=["finance", "business_analysis", "finance"],
                owner_department="finance", business_category="finance_compliance",
                content="# 概述\n跨部门适用内容。"))
            # 去重保序
            self.assertEqual(
                s.find_documents_by_applicable_department("finance"), ["D-M"])
            self.assertEqual(
                s.find_documents_by_applicable_department("business_analysis"), ["D-M"])
            self.assertEqual(
                s.find_documents_by_applicable_department("administration"), [])
        finally:
            s.close()

    def test_new_data_without_legacy_label_keeps_empty_applicability(self):
        """无任何依据时不臆造适用范围（不伪造组织归属）。"""
        s = KnowledgeStore()
        try:
            s.ingest(Document(document_id="D-X", title="未知来源", version="1",
                              content="# 概述\n未知归属内容。"))
            r = s.retrieve("未知归属")[0]
            self.assertEqual(r.applicable_departments, [])
            self.assertEqual(r.owner_department, "")
            self.assertEqual(r.business_category, "")
            self.assertEqual(r.access_scope, "company_public")
        finally:
            s.close()


class TestCLI(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    CLI = ROOT / "scripts/migrate_knowledge_metadata.py"

    def test_cli_migrate_copy(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "kb.db")
            conn = sqlite3.connect(db)
            conn.executescript(OLD_SCHEMA)
            conn.executemany(
                "INSERT INTO documents(document_id,title,version,source,status) "
                "VALUES(?,?,?,?,?)",
                # arxiv-1 故意设为 draft，验证回填真实更新路径
                [("arxiv-1", "P1", "2020", "https://arxiv.org/abs/1", "draft"),
                 ("doc-x", "PX", "2021", "file://internal", "draft")])
            conn.commit()
            conn.close()

            def run(*a):
                return subprocess.run(
                    [sys.executable, str(self.CLI), "--db", db, *a],
                    capture_output=True, text=True)

            r1 = run("--dry-run")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("added_columns", r1.stdout)

            r2 = run()
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn('"updated": 1', r2.stdout)

            r3 = run()
            self.assertEqual(r3.returncode, 0, r3.stderr)
            self.assertIn('"updated": 0', r3.stdout)

            conn = sqlite3.connect(db)
            self.assertEqual(doc_values(conn, "arxiv-1"),
                             ("public", None, "research", "active"))
            # 非语料文档在 corpus 默认 scope 下不被改动
            self.assertEqual(doc_values(conn, "doc-x"),
                             ("public", None, "research", "draft"))

    def test_cli_seed_provenance_flag(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "kb.db")
            conn = sqlite3.connect(db)
            conn.executescript(OLD_SCHEMA)
            conn.executemany(
                "INSERT INTO documents(document_id,title,version,source) "
                "VALUES(?,?,?,?)",
                [("SEED-A", "种子", "1", SEED_SOURCE),
                 ("doc-x", "PX", "1", "file://internal")])
            conn.commit()
            conn.close()

            def run(*a):
                return subprocess.run(
                    [sys.executable, str(self.CLI), "--db", db, *a],
                    capture_output=True, text=True)

            r1 = run("--seed-provenance")
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertIn("seeded_synthetic_or_curated", r1.stdout)

            conn = sqlite3.connect(db)
            meta = json.loads(conn.execute(
                "SELECT metadata_json FROM documents WHERE document_id='SEED-A'"
            ).fetchone()[0])
            self.assertEqual(meta["origin"], SEED_ORIGIN)
            self.assertIs(meta["is_real_company_data"], False)
            self.assertEqual(meta["seed_label"], SEED_LABEL)
            # 非种子来源不被改动
            other = json.loads(conn.execute(
                "SELECT metadata_json FROM documents WHERE document_id='doc-x'"
            ).fetchone()[0])
            self.assertNotIn("origin", other)
            conn.close()

            r2 = run("--seed-provenance")
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn('"documents_updated": 0', r2.stdout)


if __name__ == "__main__":
    unittest.main()
