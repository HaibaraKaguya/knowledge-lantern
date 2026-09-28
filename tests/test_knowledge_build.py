"""知识库构建脚本测试 — 五部门数量 / 总量 / 分类分布 / 元数据完整 / 幂等重建。

阶段 1 增补：每条 chunk 携带必需继承元数据（META_FIELDS），图谱表已生成。
数据审计增补：1100 条测试期种子知识的出处（provenance）完整、无真实公司声明、
权限标签有效；并断言部门/场景覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
_SCRIPTS = str(Path(_ROOT) / "scripts")
_SRC = str(Path(_ROOT) / "src")
for p in (_SCRIPTS, _SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

from build_demo_knowledge import (  # noqa: E402
    CATEGORY_PLAN,
    CHUNKS_PER_DEPT,
    DEPT_DEFS,
    build,
    make_all_docs,
)
from knowledge.migrations import SEED_SOURCE  # noqa: E402
from knowledge.models import (  # noqa: E402
    ACCESS_SCOPE_COMPANY_PUBLIC,
    APPLICABILITY_FIELDS,
    BUSINESS_CATEGORIES,
    CHUNK_CONTRACT_FIELDS,
    DEPARTMENT_KEYS,
    FINANCE_COMPLIANCE_RULE,
    LEGACY_DEPARTMENT_CONTRACT,
    META_FIELDS,
    PROVENANCE_FIELDS,
    SEED_LABEL,
    SEED_LICENSE,
    SEED_ORIGIN,
)
from knowledge.store import KnowledgeStore  # noqa: E402

#: 疑似冒充真实公司数据的声明（种子正文出现即失败）。
_FORBIDDEN_REAL_CLAIMS = (
    "本公司", "我公司", "我们公司", "集团有限公司", "股份有限公司",
    "本行", "我行", "本集团", "真实公司数据", "真实经营数据",
)
#: 不应残留的“演示/虚构”产品文案。
_FORBIDDEN_DEMO_WORDS = ("演示", "虚构")

EXPECTED_DEPARTMENTS = ("经营分析", "财务合规", "人力资源", "项目与运营", "行政")
EXPECTED_CATEGORIES = (
    "系统操作", "报错排查", "参数配置", "业务流程",
    "报表查询", "月末/年末结账", "特殊边界业务",
)


class TestBuilderOutputs(unittest.TestCase):
    def test_expected_plan_dimensions(self):
        # 五部门 × 220 = 1100
        self.assertEqual(len(DEPT_DEFS), 5)
        self.assertEqual(CHUNKS_PER_DEPT, 220)
        self.assertEqual(sum(n for _, n in CATEGORY_PLAN), 220)
        categories = {c for c, _ in CATEGORY_PLAN}
        self.assertTrue({"系统操作", "报错排查", "参数配置", "业务流程",
                         "报表查询", "月末/年末结账", "特殊边界业务"} <= categories)

    def test_generated_docs_count_per_dept_and_total(self):
        docs = make_all_docs()
        self.assertEqual(len(docs), 1100)
        for dept in DEPT_DEFS:
            n = sum(1 for d in docs if d.department == dept["name"])
            self.assertEqual(n, CHUNKS_PER_DEPT, dept["name"])
        ids = [d.document_id for d in docs]
        self.assertEqual(len(set(ids)), len(ids), "document_id 必须唯一")

    def test_classification_and_scope_metadata(self):
        docs = make_all_docs()
        by_cls: dict[str, int] = {}
        for d in docs:
            by_cls[d.classification] = by_cls.get(d.classification, 0) + 1
        self.assertEqual(by_cls["public"], 990)
        self.assertEqual(by_cls["internal"], 75)
        self.assertEqual(by_cls["restricted"], 35)
        for d in docs:
            self.assertIn(d.classification, ("public", "internal", "restricted"))
            self.assertTrue(d.department)
            self.assertTrue(d.title)
            self.assertEqual(d.status, "active")
            if d.classification == "public":
                self.assertIsNone(d.project_id, d.document_id)
            else:
                self.assertEqual(d.project_id, "finance-ledger", d.document_id)
                self.assertEqual(d.business_domain, "finance", d.document_id)

    def test_each_doc_maps_to_exactly_one_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "knowledge.db")
            report = build(db, force=True)
            self.assertEqual(report["counts"]["documents"], 1100)
            self.assertEqual(report["counts"]["chunks"], 1100)
            self.assertEqual(report["demo_chunks_missing_metadata"], 0)
            self.assertGreater(report["graph"]["nodes"], 0)
            self.assertGreater(report["graph"]["edges"], 0)
            store = KnowledgeStore(db)
            try:
                docs = store.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                chunks = store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                self.assertEqual(docs, 1100)
                self.assertEqual(chunks, 1100)
                # 每部门 220 chunk，且 chunk 继承必需元数据（META_FIELDS 全量存在）
                per_dept = dict(store.conn.execute(
                    "SELECT department, COUNT(*) FROM chunks c "
                    "JOIN documents d ON d.document_id = c.document_id "
                    "GROUP BY d.department").fetchall())
                self.assertEqual(set(per_dept), {d["name"] for d in DEPT_DEFS})
                for dept, n in per_dept.items():
                    self.assertEqual(n, 220, dept)
                empty_meta = store.conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE metadata_json = '{}'").fetchone()[0]
                self.assertEqual(empty_meta, 0)
                sample = json.loads(store.conn.execute(
                    "SELECT metadata_json FROM chunks LIMIT 1").fetchone()[0])
                for key in META_FIELDS:
                    self.assertIn(key, sample)
                nodes = store.conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                edges = store.conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
                self.assertEqual(nodes, report["graph"]["nodes"])
                self.assertEqual(edges, report["graph"]["edges"])
            finally:
                store.close()

    def test_rebuild_is_idempotent_counting(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "knowledge.db")
            r1 = build(db, force=True)
            r2 = build(db, force=True)
            self.assertEqual(r1["counts"]["documents"], 1100)
            self.assertEqual(r2["counts"]["documents"], 1100)
            self.assertEqual(r2["counts"], r1["counts"])


class TestSeedProvenance(unittest.TestCase):
    """测试期种子知识出处：完整、明确非真实公司数据、无残留演示文案。"""

    def test_every_doc_carries_seed_provenance(self):
        for d in make_all_docs():
            meta = d.metadata
            for key in PROVENANCE_FIELDS:
                self.assertIn(key, meta, f"{d.document_id} 缺 provenance 键 {key}")
            self.assertEqual(meta["origin"], SEED_ORIGIN, d.document_id)
            self.assertEqual(meta["license"], SEED_LICENSE, d.document_id)
            self.assertIs(meta["is_real_company_data"], False, d.document_id)
            self.assertEqual(meta["seed_label"], SEED_LABEL, d.document_id)

    def test_no_real_company_claims_or_demo_wording(self):
        for d in make_all_docs():
            for bad in _FORBIDDEN_REAL_CLAIMS:
                self.assertNotIn(bad, d.content, f"{d.document_id} 出现真实公司声明 {bad}")
            for bad in _FORBIDDEN_DEMO_WORDS:
                self.assertNotIn(bad, d.content, f"{d.document_id} 残留演示文案 {bad}")

    def test_department_and_scenario_coverage(self):
        docs = make_all_docs()
        by_dept: dict[str, int] = {}
        categories = set()
        for d in docs:
            by_dept[d.department] = by_dept.get(d.department, 0) + 1
            categories.add(d.metadata.get("category", ""))
        self.assertEqual(set(by_dept), set(EXPECTED_DEPARTMENTS))
        for dept in EXPECTED_DEPARTMENTS:
            self.assertEqual(by_dept[dept], 220, dept)
        self.assertTrue(set(EXPECTED_CATEGORIES) <= categories,
                        f"缺场景: {set(EXPECTED_CATEGORIES) - categories}")

    def test_permission_metadata_still_valid(self):
        for d in make_all_docs():
            self.assertIn(d.classification, ("public", "internal", "restricted"))
            self.assertEqual(d.status, "active")
            if d.classification == "public":
                self.assertIsNone(d.project_id, d.document_id)
            else:
                self.assertEqual(d.project_id, "finance-ledger", d.document_id)
                self.assertEqual(d.business_domain, "finance", d.document_id)

    def test_built_db_has_complete_provenance_in_docs_and_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "knowledge.db")
            report = build(db, force=True)
            self.assertEqual(report["seed_chunks_missing_provenance"], 0)
            self.assertEqual(report["provenance"]["origin"], SEED_ORIGIN)
            self.assertIs(report["provenance"]["is_real_company_data"], False)
            store = KnowledgeStore(db)
            try:
                for table in ("documents", "chunks"):
                    missing = 0
                    for (mj,) in store.conn.execute(
                            f"SELECT metadata_json FROM {table}").fetchall():
                        meta = json.loads(mj or "{}")
                        if any(meta.get(k) != v
                               for k, v in report["provenance"].items()
                               if k != "fields"):
                            missing += 1
                    self.assertEqual(missing, 0, table)
            finally:
                store.close()


class TestContentContract(unittest.TestCase):
    """内容适用范围契约：1100 文档/chunk 新字段完整、多选列表、与授权不混淆、
    财务合规分类规则明确、chunk/图继承、旧 department 可读。"""

    def test_every_doc_carries_applicability_contract(self):
        for d in make_all_docs():
            self.assertEqual(d.access_scope in (ACCESS_SCOPE_COMPANY_PUBLIC, "finance-ledger"),
                             True, d.document_id)
            self.assertIn(d.owner_department, DEPARTMENT_KEYS, d.document_id)
            self.assertIn(d.business_category, BUSINESS_CATEGORIES, d.document_id)
            self.assertIsInstance(d.applicable_departments, list, d.document_id)
            self.assertTrue(d.applicable_departments, d.document_id)
            for key in d.applicable_departments:
                self.assertIn(key, DEPARTMENT_KEYS, d.document_id)

    def test_applicable_departments_multi_select(self):
        docs = make_all_docs()
        multi = [d for d in docs if len(d.applicable_departments) > 1]
        self.assertTrue(multi, "适用部门必须支持多选（至少一条 > 1 个部门）")
        for d in multi:
            self.assertGreaterEqual(len(set(d.applicable_departments)), 2)

    def test_permission_not_confused_with_applicability(self):
        """旧标签/适用部门/业务分类绝不作为权限取值。"""
        for d in make_all_docs():
            self.assertNotIn(d.department, ("public", "internal", "restricted"))
            self.assertNotIn(d.access_scope, DEPARTMENT_KEYS)
            self.assertNotIn(d.business_category, ("public", "internal", "restricted"))
            for key in d.applicable_departments:
                self.assertNotIn(key, ("public", "internal", "restricted",
                                       ACCESS_SCOPE_COMPANY_PUBLIC, "finance-ledger"))
            # 授权范围键与分类/项目一致
            if d.classification == "public":
                self.assertEqual(d.access_scope, ACCESS_SCOPE_COMPANY_PUBLIC)
            else:
                self.assertEqual(d.access_scope, d.project_id)

    def test_finance_compliance_classification_rule(self):
        docs = [d for d in make_all_docs() if d.department == "财务合规"]
        self.assertEqual(len(docs), 220)
        for d in docs:
            self.assertEqual(d.business_category, "finance_compliance", d.document_id)
            self.assertEqual(d.owner_department, "finance", d.document_id)
            self.assertEqual(set(d.applicable_departments),
                             {"finance", "business_analysis"}, d.document_id)
            # 权限只由授权字段表达，绝不使用「财务合规」
            self.assertIn(d.classification, ("public", "internal", "restricted"))
            if d.classification != "public":
                self.assertEqual(d.access_scope, "finance-ledger", d.document_id)
        self.assertFalse(FINANCE_COMPLIANCE_RULE["is_department"])
        self.assertFalse(FINANCE_COMPLIANCE_RULE["is_permission"])

    def test_legacy_department_readable_and_mapped(self):
        for d in make_all_docs():
            self.assertIn(d.department, LEGACY_DEPARTMENT_CONTRACT)
            mapping = LEGACY_DEPARTMENT_CONTRACT[d.department]
            self.assertEqual(d.business_category, mapping["business_category"])
            self.assertEqual(d.owner_department, mapping["owner_department"])
            self.assertEqual(set(d.applicable_departments),
                             set(mapping["applicable_departments"]))

    def test_built_db_chunk_and_graph_inherit_contract(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "knowledge.db")
            report = build(db, force=True)
            cc = report["content_contract"]
            self.assertEqual(cc["docs_missing_contract_fields"], 0)
            self.assertEqual(cc["chunks_missing_contract_fields"], 0)
            self.assertEqual(cc["graph_rows_missing_contract_fields"], 0)
            self.assertGreater(cc["documents_with_multi_applicable_departments"], 0)
            self.assertGreater(cc["applicable_department_index_rows"], 0)
            store = KnowledgeStore(db)
            try:
                # chunk 继承同一契约
                sample = json.loads(store.conn.execute(
                    "SELECT metadata_json FROM chunks LIMIT 1").fetchone()[0])
                for key in APPLICABILITY_FIELDS + ("access_scope",):
                    self.assertIn(key, sample, key)
                self.assertIsInstance(sample["applicable_departments"], list)
                # 图谱节点/边继承同一契约
                for table in ("graph_nodes", "graph_edges"):
                    meta = json.loads(store.conn.execute(
                        f"SELECT meta_json FROM {table} LIMIT 1").fetchone()[0])
                    for key in APPLICABILITY_FIELDS + ("access_scope",):
                        self.assertIn(key, meta, f"{table}:{key}")
                # 规范化索引表可被后端按部门键过滤
                self.assertTrue(
                    store.find_documents_by_applicable_department("finance"))
                # 全量契约字段（含 META_FIELDS）在 chunk 快照中齐备
                for key in CHUNK_CONTRACT_FIELDS:
                    self.assertIn(key, sample, key)
            finally:
                store.close()

    def test_report_content_contract_mapping_and_finance_rule(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "knowledge.db")
            report = build(db, force=True)
            cc = report["content_contract"]
            self.assertEqual(
                set(cc["legacy_department_mapping"]), set(LEGACY_DEPARTMENT_CONTRACT))
            self.assertEqual(cc["finance_compliance_rule"]["business_category"],
                             "finance_compliance")
            self.assertFalse(cc["finance_compliance_rule"]["is_permission"])


class TestSeedKnowledgeLiveDB(unittest.TestCase):
    """对真实 data/knowledge.db 的 1100 条种子知识只读断言（文件存在时执行）。"""

    DB = Path(_ROOT) / "data" / "knowledge.db"

    def _ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @unittest.skipUnless(DB.exists(), "data/knowledge.db 不存在，跳过")
    def test_seed_counts_and_coverage(self):
        conn = self._ro()
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source = ?",
                (SEED_SOURCE,)).fetchone()[0]
            self.assertEqual(n, 1100)
            per_dept = dict(conn.execute(
                "SELECT department, COUNT(*) FROM documents WHERE source = ? "
                "GROUP BY department", (SEED_SOURCE,)).fetchall())
            self.assertEqual(set(per_dept), set(EXPECTED_DEPARTMENTS))
            for dept in EXPECTED_DEPARTMENTS:
                self.assertEqual(per_dept[dept], 220, dept)
            cats = {json.loads(r[0]).get("category") for r in conn.execute(
                "SELECT metadata_json FROM documents WHERE source = ?",
                (SEED_SOURCE,)).fetchall()}
            self.assertTrue(set(EXPECTED_CATEGORIES) <= cats,
                            f"缺场景: {set(EXPECTED_CATEGORIES) - cats}")
        finally:
            conn.close()

    @unittest.skipUnless(DB.exists(), "data/knowledge.db 不存在，跳过")
    def test_seed_provenance_complete_at_rest(self):
        conn = self._ro()
        try:
            doc_rows = conn.execute(
                "SELECT document_id AS id, metadata_json FROM documents "
                "WHERE source = ?", (SEED_SOURCE,)).fetchall()
            chunk_rows = conn.execute(
                "SELECT c.chunk_id AS id, c.metadata_json FROM chunks c "
                "JOIN documents d ON d.document_id = c.document_id "
                "WHERE d.source = ?", (SEED_SOURCE,)).fetchall()
            self.assertEqual(len(doc_rows), 1100)
            self.assertEqual(len(chunk_rows), 1100)
            for r in list(doc_rows) + list(chunk_rows):
                meta = json.loads(r["metadata_json"] or "{}")
                self.assertEqual(meta.get("origin"), SEED_ORIGIN, r["id"])
                self.assertIs(meta.get("is_real_company_data"), False, r["id"])
                self.assertTrue(meta.get("license"), r["id"])
                self.assertEqual(meta.get("seed_label"), SEED_LABEL, r["id"])
        finally:
            conn.close()

    @unittest.skipUnless(DB.exists(), "data/knowledge.db 不存在，跳过")
    def test_seed_permission_and_wording(self):
        conn = self._ro()
        try:
            bad_scope = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source = ? AND ("
                "(classification = 'public' AND project_id IS NOT NULL) OR "
                "(classification != 'public' AND (project_id IS NOT 'finance-ledger' "
                "OR business_domain != 'finance')) OR "
                "classification NOT IN ('public','internal','restricted'))",
                (SEED_SOURCE,)).fetchone()[0]
            self.assertEqual(bad_scope, 0)
            for bad in _FORBIDDEN_REAL_CLAIMS + _FORBIDDEN_DEMO_WORDS:
                hits = conn.execute(
                    "SELECT COUNT(*) FROM chunks c JOIN documents d "
                    "ON d.document_id = c.document_id "
                    "WHERE d.source = ? AND c.text LIKE ?",
                    (SEED_SOURCE, f"%{bad}%")).fetchone()[0]
                self.assertEqual(hits, 0, f"种子正文出现 {bad}")
        finally:
            conn.close()



    @unittest.skipUnless(DB.exists(), "data/knowledge.db 不存在，跳过")
    def test_seed_content_contract_complete_at_rest(self):
        """1100 种子文档/chunk 在静止态携带完整内容适用范围契约（旧库迁移后同样成立）。"""
        conn = self._ro()
        try:
            rows = conn.execute(
                "SELECT document_id, department, access_scope, owner_department, "
                "business_category, applicable_departments_json FROM documents "
                "WHERE source = ?", (SEED_SOURCE,)).fetchall()
            self.assertEqual(len(rows), 1100)
            multi = 0
            for r in rows:
                self.assertTrue(r["access_scope"], r["document_id"])
                self.assertIn(r["owner_department"], DEPARTMENT_KEYS, r["document_id"])
                self.assertIn(r["business_category"], BUSINESS_CATEGORIES,
                              r["document_id"])
                applicable = json.loads(r["applicable_departments_json"] or "[]")
                self.assertIsInstance(applicable, list, r["document_id"])
                self.assertTrue(applicable, r["document_id"])
                for key in applicable:
                    self.assertIn(key, DEPARTMENT_KEYS, r["document_id"])
                if len(applicable) > 1:
                    multi += 1
            self.assertGreater(multi, 0, "适用部门必须支持多选")
            # 财务合规规则在静止态成立
            fc = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE source = ? AND "
                "department = '财务合规' AND business_category = 'finance_compliance' "
                "AND owner_department = 'finance'", (SEED_SOURCE,)).fetchone()[0]
            self.assertEqual(fc, 220)
            # 规范化索引表存在且可过滤
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("document_applicable_departments", tables)
            n_index = conn.execute(
                "SELECT COUNT(*) FROM document_applicable_departments a "
                "JOIN documents d ON d.document_id = a.document_id "
                "WHERE d.source = ?", (SEED_SOURCE,)).fetchone()[0]
            self.assertGreater(n_index, 0)
            # chunk 继承同一契约
            chunk_meta = json.loads(conn.execute(
                "SELECT c.metadata_json FROM chunks c JOIN documents d "
                "ON d.document_id = c.document_id WHERE d.source = ? LIMIT 1",
                (SEED_SOURCE,)).fetchone()[0])
            for key in APPLICABILITY_FIELDS + ("access_scope",):
                self.assertIn(key, chunk_meta, key)
        finally:
            conn.close()

    @unittest.skipUnless(DB.exists(), "data/knowledge.db 不存在，跳过")
    def test_live_db_old_department_and_grants_stable(self):
        """旧 department 可读、权限与 document_id 稳定（迁移不改动这些）。"""
        conn = self._ro()
        try:
            per_dept = dict(conn.execute(
                "SELECT department, COUNT(*) FROM documents WHERE source = ? "
                "GROUP BY department", (SEED_SOURCE,)).fetchall())
            self.assertEqual(set(per_dept), set(EXPECTED_DEPARTMENTS))
            for dept in EXPECTED_DEPARTMENTS:
                self.assertEqual(per_dept[dept], 220, dept)
            by_cls = dict(conn.execute(
                "SELECT classification, COUNT(*) FROM documents WHERE source = ? "
                "GROUP BY classification", (SEED_SOURCE,)).fetchall())
            self.assertEqual(by_cls, {"public": 990, "internal": 75,
                                      "restricted": 35})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
