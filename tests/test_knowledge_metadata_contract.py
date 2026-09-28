"""元数据契约测试 — 授权/适用范围分离、真实资料入库必填契约、数据字典。

覆盖：
  1) 授权字段与适用范围字段互斥（不混淆），旧的 department 为历史字段；
  2) applicable_departments 为多选列表，normalize 去重保序；
  3) derive_access_scope 的确定性与 fail-closed 缺口标记；
  4) validate_ingest_metadata：真实资料（user_submitted / controlled_import）
     必填字段、取值白名单；种子/未知来源不强制适用范围；
  5) load_document_from_files 对真实资料执行契约校验；
  6) 数据字典结构（唯一来源 = knowledge.models 常量 + audit.build_data_dictionary）；
  7) provenance 不被伪装为真实公司数据。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (_ROOT / "src", _ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from knowledge.metadata import (  # noqa: E402
    MetadataError,
    inherit_metadata,
    load_document_from_files,
    validate_ingest_metadata,
)
from knowledge.models import (  # noqa: E402
    ACCESS_SCOPE_COMPANY_PUBLIC,
    APPLICABILITY_FIELDS,
    AUTH_FIELDS,
    BUSINESS_CATEGORIES,
    CHUNK_CONTRACT_FIELDS,
    DEPARTMENT_KEYS,
    Document,
    FINANCE_COMPLIANCE_RULE,
    LEGACY_DEPARTMENT_CONTRACT,
    ORIGIN_CONTROLLED_IMPORT,
    ORIGIN_USER_SUBMITTED,
    REAL_MATERIAL_REQUIRED_FIELDS,
    SEED_ORIGIN,
    derive_access_scope,
    normalize_applicable_departments,
)


def _real_material(**overrides) -> dict:
    base = {
        "document_id": "USER-1",
        "title": "真实资料样例",
        "version": "1",
        "status": "active",
        "classification": "internal",
        "access_scope": "finance-ledger",
        "project_id": "finance-ledger",
        "business_domain": "finance",
        "owner_department": "finance",
        "business_category": "finance_compliance",
        "applicable_departments": ["finance", "business_analysis"],
        "origin": ORIGIN_USER_SUBMITTED,
        "license": "user-provided-internal-use",
        "is_real_company_data": True,
    }
    base.update(overrides)
    return base


class TestContractSeparation(unittest.TestCase):
    def test_authorization_and_applicability_are_disjoint(self):
        overlap = set(AUTH_FIELDS) & set(APPLICABILITY_FIELDS)
        self.assertEqual(overlap, set(),
                         f"授权字段与适用范围字段不得重叠: {overlap}")
        for field in APPLICABILITY_FIELDS:
            self.assertNotIn(field, AUTH_FIELDS)
        # 旧 department 必须不在适用范围字段里（它是历史字段，非新契约）
        self.assertNotIn("department", APPLICABILITY_FIELDS)

    def test_chunk_contract_fields_cover_both_layers(self):
        for key in list(AUTH_FIELDS) + list(APPLICABILITY_FIELDS):
            self.assertIn(key, CHUNK_CONTRACT_FIELDS, key)

    def test_department_keys_and_business_categories_valid(self):
        for key in DEPARTMENT_KEYS:
            self.assertNotIn(key, ("public", "internal", "restricted"))
        self.assertIn("finance_compliance", BUSINESS_CATEGORIES)

    def test_finance_compliance_rule_not_a_permission(self):
        rule = LEGACY_DEPARTMENT_CONTRACT["财务合规"]
        self.assertFalse(rule["business_category"] in ("public", "internal", "restricted"))
        self.assertFalse(FINANCE_COMPLIANCE_RULE["is_department"])
        self.assertFalse(FINANCE_COMPLIANCE_RULE["is_permission"])
        self.assertEqual(FINANCE_COMPLIANCE_RULE["owner_department"], "finance")
        self.assertEqual(set(FINANCE_COMPLIANCE_RULE["applicable_departments"]),
                         {"finance", "business_analysis"})


class TestNormalizeAndDerive(unittest.TestCase):
    def test_normalize_applicable_departments(self):
        self.assertEqual(normalize_applicable_departments(None), [])
        self.assertEqual(normalize_applicable_departments(""), [])
        self.assertEqual(normalize_applicable_departments("finance"), ["finance"])
        self.assertEqual(
            normalize_applicable_departments(["finance", "finance", "business_analysis"]),
            ["finance", "business_analysis"])
        self.assertEqual(normalize_applicable_departments([" finance ", ""]),
                         ["finance"])

    def test_derive_access_scope(self):
        self.assertEqual(derive_access_scope("public", None),
                         ACCESS_SCOPE_COMPANY_PUBLIC)
        self.assertEqual(derive_access_scope("internal", "finance-ledger"),
                         "finance-ledger")
        # fail-closed 缺口标记：非 public 且无项目
        self.assertEqual(derive_access_scope("restricted", None),
                         "unscoped_restricted")


class TestRealMaterialContract(unittest.TestCase):
    def test_complete_real_material_passes(self):
        meta = _real_material()
        self.assertIs(validate_ingest_metadata(meta), meta)
        for origin in (ORIGIN_USER_SUBMITTED, ORIGIN_CONTROLLED_IMPORT):
            result = validate_ingest_metadata(_real_material(origin=origin))
            self.assertEqual(result["origin"], origin)

    def test_missing_required_fields_rejected(self):
        for field in REAL_MATERIAL_REQUIRED_FIELDS:
            meta = _real_material()
            meta.pop(field)
            with self.assertRaises(MetadataError, msg=field):
                validate_ingest_metadata(meta)

    def test_empty_applicable_departments_rejected(self):
        with self.assertRaises(MetadataError):
            validate_ingest_metadata(_real_material(applicable_departments=[]))

    def test_applicable_departments_must_be_list(self):
        with self.assertRaises(MetadataError):
            validate_ingest_metadata(_real_material(applicable_departments="finance"))

    def test_bad_values_rejected(self):
        with self.assertRaises(MetadataError):
            validate_ingest_metadata(_real_material(business_category="unknown_cat"))
        with self.assertRaises(MetadataError):
            validate_ingest_metadata(_real_material(owner_department="unknown_dept"))
        with self.assertRaises(MetadataError):
            validate_ingest_metadata(_real_material(applicable_departments=["nope"]))

    def test_seed_origin_not_forced_to_have_applicability(self):
        """种子/未知来源不会被强制适用范围（保持旧库兼容），但取值一旦提供即校验。"""
        meta = {"document_id": "S-1", "title": "t", "version": "1",
                "origin": SEED_ORIGIN}
        self.assertIs(validate_ingest_metadata(meta), meta)
        with self.assertRaises(MetadataError):
            validate_ingest_metadata({"document_id": "S-2", "title": "t",
                                      "version": "1", "origin": SEED_ORIGIN,
                                      "business_category": "bogus"})

    def test_provenance_not_disguised_for_unknown_source(self):
        """未知来源只补缺省 provenance（真实性未知），绝不写成种子出处。"""
        doc = Document(document_id="U-1", title="t", version="1",
                       content="# 概述\n正文。")
        meta = inherit_metadata(doc)
        self.assertEqual(meta["origin"], "unspecified")
        self.assertIsNone(meta["is_real_company_data"])
        self.assertNotEqual(meta["origin"], SEED_ORIGIN)

    def test_load_document_from_files_enforces_real_material(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 真实资料但缺适用范围 → 拒绝
            (d / "metadata.json").write_text(json.dumps({
                "document_id": "USER-1", "title": "t", "version": "1",
                "origin": ORIGIN_USER_SUBMITTED, "license": "x",
                "is_real_company_data": True,
            }, ensure_ascii=False), encoding="utf-8")
            (d / "content.md").write_text("# 标题\n正文。", encoding="utf-8")
            with self.assertRaises(MetadataError):
                load_document_from_files(d)
            # 补全后通过，并读入适用范围字段
            (d / "metadata.json").write_text(json.dumps(
                _real_material(), ensure_ascii=False), encoding="utf-8")
            doc = load_document_from_files(d)
            self.assertEqual(doc.applicable_departments,
                             ["finance", "business_analysis"])
            self.assertEqual(doc.owner_department, "finance")
            self.assertEqual(doc.business_category, "finance_compliance")
            self.assertEqual(doc.access_scope, "finance-ledger")


class TestDataDictionary(unittest.TestCase):
    def test_dictionary_shape(self):
        import audit_knowledge_seed as audit
        dd = audit.build_data_dictionary()
        self.assertEqual(dd["version"], "2.0")
        self.assertEqual(set(dd["layers"]), {
            "authorization", "applicability", "descriptive_inherited", "provenance"})
        self.assertEqual(dd["layers"]["authorization"]["fields"], list(AUTH_FIELDS))
        self.assertEqual(dd["layers"]["applicability"]["fields"],
                         list(APPLICABILITY_FIELDS))
        self.assertEqual(dd["legacy_department_mapping"]["财务合规"]["business_category"],
                         "finance_compliance")
        self.assertEqual(dd["real_material_ingest_contract"]["required_fields"],
                         list(REAL_MATERIAL_REQUIRED_FIELDS))
        self.assertEqual(
            dd["storage"]["applicable_department_index"]["table"],
            "document_applicable_departments")

    def test_dictionary_single_source_matches_models(self):
        import audit_knowledge_seed as audit
        dd = audit.build_data_dictionary()
        self.assertEqual(set(dd["legacy_department_mapping"]),
                         set(LEGACY_DEPARTMENT_CONTRACT))
        self.assertEqual(dd["chunk_contract_fields"], list(CHUNK_CONTRACT_FIELDS))


if __name__ == "__main__":
    unittest.main()
