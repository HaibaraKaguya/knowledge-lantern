#!/usr/bin/env python
"""测试期种子知识审计 — 逐条审计 1100 chunk，输出 JSON + Markdown 报告。

只读审计（绝不修改 data/knowledge.db）：
  - 结构/元数据/文本统计走 sqlite3 只读连接（file:...?mode=ro）；
  - 检索覆盖在数据库的临时副本上用既有 KnowledgeStore / evaluate_retrieval 运行，
    不改动检索运行代码，也不触碰原库。

审计维度（逐条 + 聚合，抽样列出问题 ID）：
  1. 总量与覆盖：1100 文档/chunk、五类旧部门标签各 220、七类场景齐全；
  2. 标题/正文重复：完全重复标题、完全重复正文、去数字骨架重复（模板化）；
  3. 过短/模板化：正文长度分布、过短阈值、骨架分组占比；
  4. 元数据缺失：文档级 META_FIELDS、chunk 级继承元数据；
  5. 权限标签：classification 白名单、access_scope 与 classification/project_id 一致、
     public↔project_id=None、internal/restricted↔finance-ledger/finance 一致性；
  6. 内容适用范围新契约：applicable_departments（多选列表）/ owner_department /
     business_category 完整、取值合法、与权限字段不混淆、「财务合规」分类规则明确；
  7. 来源/许可：source 分布、PROVENANCE_FIELDS 完整性、license 取值；
  8. “演示/虚构”字样与真实公司声明：正文关键词扫描；
  9. chunk/图谱快照继承同一契约；检索覆盖：FTS 索引完整率 + 评测问题 hit@k；
  10. 结论：是否可作为测试期种子知识（逐项 pass/warn/fail）。

用法:
  .venv/bin/python scripts/audit_knowledge_seed.py
  .venv/bin/python scripts/audit_knowledge_seed.py \
      --db data/knowledge.db \
      --json-out data/knowledge_seed_audit.json \
      --md-out data/knowledge_seed_audit.md \
      --dictionary-out data/knowledge_metadata_dictionary.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_PROJECT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from knowledge.models import (  # noqa: E402
    APPLICABILITY_FIELDS, AUTH_FIELDS, BUSINESS_CATEGORIES, CHUNK_CONTRACT_FIELDS,
    CLASSIFICATIONS, DEPARTMENT_KEYS, DEPARTMENT_REGISTRY, FINANCE_COMPLIANCE_RULE,
    LEGACY_DEPARTMENT_CONTRACT, META_FIELDS, PROVENANCE_FIELDS,
    REAL_MATERIAL_REQUIRED_FIELDS, SEED_LICENSE, SEED_ORIGIN,
    derive_access_scope, normalize_applicable_departments,
)
from knowledge.migrations import SEED_SOURCE  # noqa: E402

#: 内容适用范围契约键（+ access_scope）：documents/chunk/图谱快照都必须携带。
CONTRACT_KEYS: tuple[str, ...] = ("access_scope", *APPLICABILITY_FIELDS)

#: 判定阈值（可审阅、可调整）
MIN_BODY_CHARS = 40          # 正文短于此视为过短
MIN_HIT_RATE_AT_5 = 0.80     # 评测问题 hit@5 的参考下限（advisory，不作硬门槛）
EXPECTED_DOCS = 1100
EXPECTED_PER_DEPT = 220
EXPECTED_DEPARTMENTS = ("经营分析", "财务合规", "人力资源", "项目与运营", "行政")
EXPECTED_CATEGORIES = (
    "系统操作", "报错排查", "参数配置", "业务流程",
    "报表查询", "月末/年末结账", "特殊边界业务",
)

#: 授权字段的单一事实源是 documents 列（非 metadata_json），文档级不要求 JSON 里带这些键。
AUTH_COLUMN_FIELDS = ("department", "classification", "project_id",
                      "business_domain", "status")
#: 文档级 metadata_json 应具备的描述性字段。
DOC_DESCRIPTIVE_FIELDS = tuple(
    k for k in META_FIELDS if k not in AUTH_COLUMN_FIELDS)

#: 疑似“冒充真实公司数据”的声明模式（正文出现即视为风险）。
_REAL_COMPANY_PATTERNS = (
    "本公司", "我公司", "我们公司", "集团有限公司", "股份有限公司",
    "本行", "我行", "本集团", "真实公司数据", "真实经营数据",
)
#: “演示/虚构”字样（产品文案残留检查）。
_DEMO_WORDS = ("演示", "虚构", "样例数据", "占位数据")
#: 敏感条目必须携带的“非真实数据”免责声明片段。
_DISCLAIMER_MARKERS = ("不代表任何真实", "合成占位")

#: 备份 / 重建命令（写入报告，供复核与复现；与 README §6.1 一致）。
BACKUP_COMMAND = "cp data/knowledge.db data/knowledge.db.bak-$(date +%Y%m%d-%H%M%S)"
REBUILD_COMMANDS = (
    "# 增量覆盖重建种子条目（非破坏性，ID/权限稳定）\n"
    "PYTHONPATH=src .venv/bin/python scripts/build_demo_knowledge.py "
    "--db data/knowledge.db",
    "# 已有库原地补种子出处字段（只增键、幂等）\n"
    "PYTHONPATH=src .venv/bin/python scripts/migrate_knowledge_metadata.py "
    "--db data/knowledge.db --seed-provenance",
    "# 已有库原地补内容适用范围契约（列/索引表/小快照回填，幂等）\n"
    "PYTHONPATH=src .venv/bin/python scripts/migrate_knowledge_metadata.py "
    "--db data/knowledge.db",
    "# 重建向量/图谱索引资产（离线，约 5 分钟）\n"
    "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=src "
    ".venv/bin/python scripts/build_knowledge_indices.py --db data/knowledge.db",
    "# 重新生成本审计报告（只读）\n"
    "PYTHONPATH=src .venv/bin/python scripts/audit_knowledge_seed.py "
    "--db data/knowledge.db",
)

#: 本次审计发现、但不在允许修改范围内的陈旧/错误产品文案（只报告，不改）。
OUT_OF_SCOPE_FINDINGS = (
    {
        "path": "static/index.html",
        "issue": "页面文案仍称“当前可检索语料为 50 篇 RAG / GraphRAG 论文"
                 "（public/research 演示数据）”，与现状（1100 条测试期种子知识）"
                 "不符；static/ 为只读边界，未修改。",
    },
    {
        "path": "static/js/chart.js",
        "issue": "图卡 badge 文案为“演示数据 / 尚未接入业务数据”；未修改。",
    },
    {
        "path": "README.md §1",
        "issue": "产品边界仍列出“论文 RAG / GraphRAG / BGE-M3 向量 / Reranker …"
                 "已物理移除”，但相应模块已恢复存在；§1 不在本次“数据章节”"
                 "允许修改范围内，未修改。",
    },
    {
        "path": "work/run-paper-qa-web.sh",
        "issue": "注释仍写“knowledge.db = paper corpus”；work/ 为只读边界，未修改。",
    },
)


def _readonly_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _skeleton(text: str, variables: tuple = ()) -> str:
    """模板骨架：掩掉变量词（部门/模块/主题）与数字，用于识别模板化正文。

    合成种子按 7 类场景共用句式，仅以 部门/模块/主题/数值 区分；因此掩掉这些
    变量后，同类条目的骨架应高度一致 —— 这正是要量化的“模板化”程度。
    """
    out = text
    for v in sorted((x for x in variables if x), key=len, reverse=True):
        out = out.replace(v, "◇")
    return re.sub(r"\d+", "#", out)


def _load_seed(conn: sqlite3.Connection, source: str) -> tuple[list[dict], list[dict]]:
    docs = [dict(r) for r in conn.execute(
        "SELECT document_id, title, version, status, classification, access_scope, "
        "project_id, business_domain, department, owner_department, "
        "business_category, applicable_departments_json, source, metadata_json "
        "FROM documents WHERE source = ? ORDER BY document_id", (source,))]
    chunks = [dict(r) for r in conn.execute(
        "SELECT c.chunk_id, c.document_id, c.text, c.heading_path, c.status, "
        "c.metadata_json FROM chunks c JOIN documents d "
        "ON d.document_id = c.document_id WHERE d.source = ? "
        "ORDER BY c.chunk_id", (source,))]
    return docs, chunks


def _load_graph_meta(conn: sqlite3.Connection, source: str) -> dict[str, list[dict]]:
    """读取种子文档的图谱节点/边 meta_json 快照（用于契约继承检查）。"""
    out: dict[str, list[dict]] = {"graph_nodes": [], "graph_edges": []}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table in out:
        if table not in tables:
            continue
        for (mj,) in conn.execute(
            f"SELECT g.meta_json FROM {table} g JOIN documents d "
            "ON d.document_id = g.document_id WHERE d.source = ?", (source,)
        ).fetchall():
            out[table].append(json.loads(mj or "{}"))
    return out


def build_data_dictionary() -> dict:
    """元数据契约数据字典（唯一来源 = knowledge.models 常量）。"""
    return {
        "version": "2.0",
        "layers": {
            "authorization": {
                "purpose": "服务端授权专用（能否检索）",
                "fields": list(AUTH_FIELDS),
                "single_source_of_truth": "documents 表列",
                "notes": "不得使用 department / applicable_departments / "
                         "business_category 作为权限依据。",
            },
            "applicability": {
                "purpose": "内容适用范围（业务按钮进入后、检索前过滤）",
                "fields": list(APPLICABILITY_FIELDS),
                "notes": "applicable_departments 为多选列表；不参与授权判定。",
            },
            "descriptive_inherited": {
                "purpose": "chunk/图谱快照（展示与离线审计）",
                "fields": list(META_FIELDS),
            },
            "provenance": {
                "purpose": "内容来源与真实性声明",
                "fields": list(PROVENANCE_FIELDS),
            },
        },
        "department_registry": dict(DEPARTMENT_REGISTRY),
        "business_categories": list(BUSINESS_CATEGORIES),
        "classification": list(CLASSIFICATIONS),
        "access_scope": {
            "company_public": "全公司可检索（= classification='public' 且 project_id IS NULL）",
            "other": "受控范围键（种子语料固定 'finance-ledger'；缺失时 'unscoped_<classification>'）",
        },
        "legacy_department_field": {
            "status": "历史/兼容字段（保留旧标签，不删除）",
            "readers": "旧数据/旧测试仍可读取；新数据必须同时写适用范围新字段",
        },
        "legacy_department_mapping": {
            k: {
                "owner_department": v["owner_department"],
                "business_category": v["business_category"],
                "applicable_departments": list(v["applicable_departments"]),
            } for k, v in LEGACY_DEPARTMENT_CONTRACT.items()
        },
        "finance_compliance_rule": {
            **FINANCE_COMPLIANCE_RULE,
            "applicable_departments": list(
                FINANCE_COMPLIANCE_RULE["applicable_departments"]),
        },
        "chunk_contract_fields": list(CHUNK_CONTRACT_FIELDS),
        "storage": {
            "documents_columns": [
                "classification", "access_scope", "project_id", "business_domain",
                "status", "owner_department", "business_category",
                "applicable_departments_json",
            ],
            "applicable_department_index": {
                "table": "document_applicable_departments",
                "columns": ["document_id", "department_key"],
                "index": "idx_applicable_dept_key",
                "purpose": "后端在检索前按部门键做可索引过滤（不改变检索运行代码）",
            },
            "chunk_graph": "chunk.metadata_json / graph_nodes.meta_json / "
                           "graph_edges.meta_json 继承同一契约快照（非授权来源）",
        },
        "real_material_ingest_contract": {
            "allowed_origins": ["user_submitted", "controlled_import"],
            "required_fields": list(REAL_MATERIAL_REQUIRED_FIELDS),
            "applied_by": "knowledge.metadata.validate_ingest_metadata",
            "notes": "真实资料必须显式声明 origin/license/is_real_company_data 与适用范围；"
                     "不得以种子 provenance 冒充真实公司数据。",
        },
    }


def _missing_keys(meta: dict, keys, allow_none: tuple = (),
                  allow_empty: tuple = ()) -> list[str]:
    """返回 meta 中缺失/空值的键。

    allow_none: 允许显式 None（如 project_id=None 表示无项目限定）。
    allow_empty: 允许空串（如 error_code 无报错时为空）。
    """
    out = []
    for k in keys:
        if k not in meta:
            out.append(k)
            continue
        v = meta[k]
        if v is None and k not in allow_none:
            out.append(k)
            continue
        if isinstance(v, str) and not v.strip() and k not in allow_empty:
            out.append(k)
    return out


def _missing_contract(meta: dict) -> bool:
    """内容适用范围契约（+access_scope）是否缺失（空值/空列表视为缺失）。"""
    for key in CONTRACT_KEYS:
        value = meta.get(key)
        if value is None or value == "" or value == []:
            return True
    return False


def _provenance_problems(meta: dict) -> list[str]:
    """种子条目的出处必须完整且明确非真实公司数据。"""
    probs: list[str] = []
    for k in PROVENANCE_FIELDS:
        if k not in meta:
            probs.append(f"missing:{k}")
    if meta.get("origin") != SEED_ORIGIN:
        probs.append("origin!=seeded_synthetic_or_curated")
    if meta.get("is_real_company_data") is not False:
        probs.append("is_real_company_data!=False")
    if not meta.get("license"):
        probs.append("license_empty")
    if not meta.get("seed_label"):
        probs.append("seed_label_empty")
    return probs


def _retrieval_coverage(db_path: Path, top_k: int, sample_limit: int) -> dict:
    """在临时副本上跑评测问题检索覆盖（不改原库、不改检索代码）。"""
    questions = []
    qfile = _PROJECT_ROOT / "data" / "knowledge_demo" / "eval_questions.json"
    if qfile.exists():
        try:
            questions = json.loads(qfile.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            questions = []
    if not questions:
        return {"available": False, "reason": "未找到评测问题文件，跳过检索覆盖"}

    from knowledge.store import KnowledgeStore
    from knowledge.eval import evaluate_retrieval

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "audit_copy.db"
        shutil.copy2(db_path, tmp)
        store = KnowledgeStore(str(tmp))
        try:
            report = evaluate_retrieval(store, questions, top_k=top_k)
            by_dept: dict[str, dict] = {}
            for s in report["details"]:
                dept = ""
                for q in questions:
                    if q["question"] == s["question"]:
                        dept = (q.get("meta") or {}).get("department", "")
                        break
                bucket = by_dept.setdefault(dept, {"samples": 0, "hits": 0})
                bucket["samples"] += 1
                bucket["hits"] += int(bool(s[f"hit@{top_k}"]))
        finally:
            store.close()

    for dept, b in by_dept.items():
        b["hit_rate"] = round(b["hits"] / b["samples"], 4) if b["samples"] else 0.0
    misses = [
        {"question": s["question"], "gold_document_id": s["gold_document_id"]}
        for s in report["details"] if not s[f"hit@{top_k}"]
    ][:sample_limit]
    return {
        "available": True,
        "questions": report["samples"],
        "top_k": top_k,
        "hit_rate": round(report[f"hit@{top_k}"], 4),
        "mrr": round(report["mrr"], 4),
        "by_department": by_dept,
        "miss_samples": misses,
    }


def audit(db_path: Path, *, source: str = SEED_SOURCE, sample_limit: int = 20,
          top_k: int = 5, retrieval: bool = True) -> dict:
    conn = _readonly_conn(db_path)
    try:
        docs, chunks = _load_seed(conn, source)
        graph_meta = _load_graph_meta(conn, source)
        n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        fts_chunk_ids = {r[0] for r in conn.execute(
            "SELECT chunk_id FROM chunks_fts")}
        total_db_docs = conn.execute(
            "SELECT COUNT(*) FROM documents").fetchone()[0]
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM documents GROUP BY status").fetchall())
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        applicable_index_rows = 0
        if "document_applicable_departments" in tables:
            applicable_index_rows = conn.execute(
                "SELECT COUNT(*) FROM document_applicable_departments a "
                "JOIN documents d ON d.document_id = a.document_id "
                "WHERE d.source = ?", (source,)).fetchone()[0]
    finally:
        conn.close()

    problems: dict[str, list[str]] = defaultdict(list)

    def _flag(check: str, doc_id: str):
        if len(problems[check]) < sample_limit:
            problems[check].append(doc_id)

    # ── 1. 覆盖 ──────────────────────────────────────────────
    by_dept = Counter(d["department"] for d in docs)
    by_class = Counter(d["classification"] for d in docs)
    by_category = Counter()
    for d in docs:
        by_category[json.loads(d["metadata_json"] or "{}").get("category", "")] += 1

    coverage_ok = (
        len(docs) == EXPECTED_DOCS and len(chunks) == EXPECTED_DOCS
        and set(by_dept) == set(EXPECTED_DEPARTMENTS)
        and all(by_dept[d] == EXPECTED_PER_DEPT for d in EXPECTED_DEPARTMENTS)
        and set(EXPECTED_CATEGORIES) <= set(by_category)
    )

    # ── 2. 重复 ──────────────────────────────────────────────
    title_dups = [t for t, n in Counter(d["title"] for d in docs).items() if n > 1]
    body_dups = [t for t, n in Counter(c["text"] for c in chunks).items() if n > 1]
    doc_meta_by_id = {d["document_id"]: json.loads(d["metadata_json"] or "{}")
                      for d in docs}
    dept_by_id = {d["document_id"]: d["department"] for d in docs}
    skeleton_counter = Counter()
    for c in chunks:
        m = doc_meta_by_id.get(c["document_id"], {})
        skeleton_counter[_skeleton(
            c["text"],
            (m.get("module", ""), m.get("topic", ""),
             dept_by_id.get(c["document_id"], "")))] += 1
    skeletons = skeleton_counter
    top_skeleton_share = (max(skeletons.values()) / len(chunks)) if chunks else 0.0
    dup_title_ids = [d["document_id"] for d in docs
                     if d["title"] in set(title_dups)][:sample_limit]
    body_dup_ids = [c["chunk_id"] for c in chunks
                    if c["text"] in set(body_dups)][:sample_limit]

    # ── 3. 长度/过短 ─────────────────────────────────────────
    lens = sorted(len(c["text"]) for c in chunks)
    too_short = [c for c in chunks if len(c["text"]) < MIN_BODY_CHARS]
    for c in too_short:
        _flag("too_short", c["chunk_id"])

    # ── 4/5/6. 元数据 / 权限 / 出处 + 内容适用范围契约 ────────
    doc_meta_missing = 0
    chunk_meta_missing = 0
    provenance_missing = 0
    chunk_provenance_missing = 0
    bad_classification: list[str] = []
    scope_mismatch: list[str] = []
    access_scope_mismatch: list[str] = []
    applicability_missing: list[str] = []
    bad_applicable_key: list[str] = []
    bad_business_category: list[str] = []
    bad_owner_department: list[str] = []
    applicable_value_misuse: list[str] = []
    legacy_dept_mismatch: list[str] = []
    multi_select_docs = 0
    provenance_values = Counter()
    license_values = Counter()
    by_owner_department = Counter()
    by_business_category = Counter()
    by_access_scope = Counter()
    for d in docs:
        meta = json.loads(d["metadata_json"] or "{}")
        if _missing_keys(meta, DOC_DESCRIPTIVE_FIELDS, allow_empty=("error_code",)):
            doc_meta_missing += 1
            _flag("doc_metadata_missing", d["document_id"])
        if _provenance_problems(meta):
            provenance_missing += 1
            _flag("provenance_missing", d["document_id"])
        provenance_values[meta.get("origin", "")] += 1
        license_values[meta.get("license", "")] += 1
        if d["classification"] not in CLASSIFICATIONS:
            bad_classification.append(d["document_id"])
            _flag("bad_classification", d["document_id"])
        if d["classification"] == "public":
            if d["project_id"] is not None:
                scope_mismatch.append(d["document_id"])
                _flag("scope_mismatch", d["document_id"])
        else:
            if d["project_id"] != "finance-ledger" or d["business_domain"] != "finance":
                scope_mismatch.append(d["document_id"])
                _flag("scope_mismatch", d["document_id"])

        # 授权范围键：只表达授权，必须与 classification/project_id 一致。
        access_scope = d["access_scope"] or meta.get("access_scope", "")
        expected_scope = derive_access_scope(d["classification"], d["project_id"])
        if access_scope != expected_scope:
            access_scope_mismatch.append(d["document_id"])
            _flag("access_scope_mismatch", d["document_id"])
        by_access_scope[access_scope] += 1

        # 内容适用范围契约（授权无关）：applicable_departments / owner_department /
        # business_category 三者必须齐备且取值合法。
        applicable_raw = d["applicable_departments_json"] or meta.get(
            "applicable_departments", "[]")
        try:
            applicable = normalize_applicable_departments(
                json.loads(applicable_raw)
                if isinstance(applicable_raw, str) else applicable_raw)
        except (TypeError, json.JSONDecodeError):
            applicable = []
        owner_department = d["owner_department"] or meta.get("owner_department", "")
        business_category = d["business_category"] or meta.get(
            "business_category", "")
        if not applicable:
            applicability_missing.append(d["document_id"])
            _flag("applicability_missing", d["document_id"])
        if len(applicable) > 1:
            multi_select_docs += 1
        for key in applicable:
            if key not in DEPARTMENT_KEYS:
                bad_applicable_key.append(d["document_id"])
                _flag("bad_applicable_key", d["document_id"])
            # 适用部门绝不可是权限取值（classification/access_scope 混入）
            if key in CLASSIFICATIONS or key in ("company_public", "finance-ledger"):
                applicable_value_misuse.append(d["document_id"])
                _flag("applicable_value_misuse", d["document_id"])
        if business_category not in BUSINESS_CATEGORIES:
            bad_business_category.append(d["document_id"])
            _flag("bad_business_category", d["document_id"])
        if owner_department not in DEPARTMENT_KEYS:
            bad_owner_department.append(d["document_id"])
            _flag("bad_owner_department", d["document_id"])
        by_owner_department[owner_department] += 1
        by_business_category[business_category] += 1

        # 旧 department 标签必须可读，且与映射一致（财务合规为业务分类规则）。
        legacy = LEGACY_DEPARTMENT_CONTRACT.get(d["department"] or "")
        if legacy:
            if (owner_department != legacy["owner_department"]
                    or business_category != legacy["business_category"]
                    or set(applicable) != set(legacy["applicable_departments"])):
                legacy_dept_mismatch.append(d["document_id"])
                _flag("legacy_department_mismatch", d["document_id"])
    for c in chunks:
        meta = json.loads(c["metadata_json"] or "{}")
        if not meta or _missing_keys(meta, META_FIELDS,
                                     allow_none=("project_id",),
                                     allow_empty=("error_code",)):
            chunk_meta_missing += 1
            _flag("chunk_metadata_missing", c["chunk_id"])
        if _provenance_problems(meta):
            chunk_provenance_missing += 1
            _flag("chunk_provenance_missing", c["chunk_id"])

    # 内容适用范围契约在 chunk / 图谱快照的继承完整性（同一契约）
    chunk_contract_missing = sum(
        1 for c in chunks if _missing_contract(json.loads(c["metadata_json"] or "{}")))
    graph_rows_total = len(graph_meta["graph_nodes"]) + len(graph_meta["graph_edges"])
    graph_contract_missing = sum(
        1 for rows in graph_meta.values() for m in rows if _missing_contract(m))

    # 「财务合规」分类规则校验：所有旧标签=财务合规 的文档必须落到
    # business_category=finance_compliance、owner_department=finance、
    # access_scope=finance-ledger（internal/restricted），且**不得**把
    # 「财务合规」写进任何权限字段。
    fc_docs = [d for d in docs if d["department"] == "财务合规"]
    fc_rule_violations = [
        d["document_id"] for d in fc_docs
        if not (d["business_category"] == "finance_compliance"
                and d["owner_department"] == "finance"
                and (d["classification"] == "public"
                     or d["access_scope"] == "finance-ledger")
                and d["classification"] in CLASSIFICATIONS)
    ]
    for did in fc_rule_violations[:sample_limit]:
        _flag("finance_compliance_rule_violation", did)
    # 权限字段绝不能等于旧部门标签（防止「财务合规」被当作权限替代）
    auth_field_leak = [
        d["document_id"] for d in docs
        if d["classification"] == "财务合规" or d["access_scope"] == "财务合规"
        or d["business_domain"] == "财务合规"
    ]
    for did in auth_field_leak[:sample_limit]:
        _flag("legacy_label_in_permission_field", did)

    # ── 7. 文案 ──────────────────────────────────────────────
    demo_word_ids, fiction_word_ids, real_claim_ids = [], [], []
    sensitive_without_disclaimer = []
    for c in chunks:
        text = c["text"]
        if any(w in text for w in _DEMO_WORDS):
            demo_word_ids.append(c["chunk_id"])
        if "虚构" in text:
            fiction_word_ids.append(c["chunk_id"])
        if any(p in text for p in _REAL_COMPANY_PATTERNS):
            real_claim_ids.append(c["chunk_id"])
    doc_by_id = {d["document_id"]: d for d in docs}
    for c in chunks:
        d = doc_by_id.get(c["document_id"])
        if d and d["classification"] in ("internal", "restricted"):
            if not any(m in c["text"] for m in _DISCLAIMER_MARKERS):
                sensitive_without_disclaimer.append(c["chunk_id"])
    for cid in demo_word_ids[:sample_limit]:
        _flag("demo_wording", cid)
    for cid in real_claim_ids[:sample_limit]:
        _flag("real_company_claim", cid)
    for cid in sensitive_without_disclaimer[:sample_limit]:
        _flag("sensitive_missing_disclaimer", cid)

    # ── 8. 检索覆盖 ──────────────────────────────────────────
    chunk_ids = {c["chunk_id"] for c in chunks}
    fts_missing = sorted(chunk_ids - fts_chunk_ids)[:sample_limit]
    fts_index_complete = len(chunk_ids - fts_chunk_ids) == 0
    retrieval_report = (
        _retrieval_coverage(db_path, top_k, sample_limit) if retrieval
        else {"available": False, "reason": "已通过 --no-retrieval 关闭"}
    )

    # ── 9. 结论 ──────────────────────────────────────────────
    # critical = 决定“能否作为测试期种子知识”的硬门槛；
    # advisory = 需记录但不阻断的已知性质（模板化、检索质量）。
    checks = {
        "counts_1100_docs_and_chunks": {
            "severity": "critical",
            "pass": len(docs) == EXPECTED_DOCS and len(chunks) == EXPECTED_DOCS,
            "detail": {"documents": len(docs), "chunks": len(chunks)},
        },
        "five_departments_220_each": {
            "severity": "critical",
            "pass": coverage_ok,
            "detail": dict(by_dept),
        },
        "seven_scenarios_present": {
            "severity": "critical",
            "pass": set(EXPECTED_CATEGORIES) <= set(by_category),
            "detail": dict(by_category),
        },
        "no_exact_duplicate_titles": {
            "severity": "critical", "pass": not title_dups},
        "no_exact_duplicate_bodies": {
            "severity": "critical", "pass": not body_dups},
        "no_too_short_bodies": {
            "severity": "critical", "pass": not too_short,
            "min_chars": MIN_BODY_CHARS},
        "doc_metadata_complete": {
            "severity": "critical", "pass": doc_meta_missing == 0},
        "chunk_metadata_complete": {
            "severity": "critical", "pass": chunk_meta_missing == 0},
        "provenance_complete": {
            "severity": "critical",
            "pass": provenance_missing == 0 and chunk_provenance_missing == 0,
            "origin_values": dict(provenance_values),
            "license_values": dict(license_values),
            "expected_origin": SEED_ORIGIN,
            "expected_license": SEED_LICENSE,
        },
        "permission_tags_valid": {
            "severity": "critical",
            "pass": not bad_classification and not scope_mismatch,
            "by_classification": dict(by_class),
        },
        "access_scope_consistent": {
            "severity": "critical",
            "pass": not access_scope_mismatch,
            "by_access_scope": dict(by_access_scope),
            "rule": "company_public ⇔ classification='public' 且 project_id IS NULL；"
                    "其余为受控范围键（种子 = finance-ledger）",
        },
        "applicability_contract_complete": {
            "severity": "critical",
            "pass": (not applicability_missing and not bad_applicable_key
                     and not bad_business_category and not bad_owner_department),
            "by_owner_department": dict(by_owner_department),
            "by_business_category": dict(by_business_category),
            "bad_applicable_key": len(bad_applicable_key),
            "bad_business_category": len(bad_business_category),
            "bad_owner_department": len(bad_owner_department),
        },
        "applicable_departments_is_multi_select_list": {
            "severity": "critical",
            "pass": multi_select_docs > 0 and applicable_index_rows > 0,
            "documents_with_multi_applicable_departments": multi_select_docs,
            "applicable_department_index_rows": applicable_index_rows,
        },
        "permission_not_confused_with_applicability": {
            "severity": "critical",
            "pass": not applicable_value_misuse and not auth_field_leak,
            "applicable_value_misuse": len(applicable_value_misuse),
            "legacy_label_in_permission_field": len(auth_field_leak),
            "rule": "applicable_departments/owner_department/business_category 不参与授权；"
                    "权限仅由 classification/access_scope/project_id/business_domain/status 表达",
        },
        "finance_compliance_classification_rule": {
            "severity": "critical",
            "pass": not fc_rule_violations and len(fc_docs) > 0,
            "finance_compliance_documents": len(fc_docs),
            "violations": len(fc_rule_violations),
            "rule": FINANCE_COMPLIANCE_RULE["rule"],
        },
        "legacy_department_readable_and_mapped": {
            "severity": "critical",
            "pass": (set(by_dept) == set(EXPECTED_DEPARTMENTS)
                     and not legacy_dept_mismatch),
            "legacy_department_values": dict(by_dept),
            "mismatch": len(legacy_dept_mismatch),
        },
        "chunk_graph_inherit_contract": {
            "severity": "critical",
            "pass": chunk_contract_missing == 0 and graph_contract_missing == 0,
            "chunk_contract_missing": chunk_contract_missing,
            "graph_rows_total": graph_rows_total,
            "graph_contract_missing": graph_contract_missing,
            "contract_fields": list(CONTRACT_KEYS),
        },
        "no_demo_or_fiction_wording": {
            "severity": "critical",
            "pass": not demo_word_ids and not fiction_word_ids,
            "demo_word_chunks": len(demo_word_ids),
            "fiction_word_chunks": len(fiction_word_ids),
        },
        "no_fake_real_company_claims": {
            "severity": "critical",
            "pass": not real_claim_ids,
            "claim_chunks": len(real_claim_ids),
        },
        "sensitive_entries_disclaimed": {
            "severity": "critical",
            "pass": not sensitive_without_disclaimer,
            "missing_disclaimer_chunks": len(sensitive_without_disclaimer),
        },
        "fts_index_complete": {"severity": "critical", "pass": fts_index_complete},
        "template_diversity": {
            "severity": "advisory",
            "pass": len(skeletons) >= 50,
            "distinct_template_skeletons": len(skeletons),
            "top_skeleton_share": round(top_skeleton_share, 4),
        },
    }
    if retrieval_report.get("available"):
        checks["retrieval_hit_rate"] = {
            "severity": "advisory",
            "pass": retrieval_report["hit_rate"] >= MIN_HIT_RATE_AT_5,
            "hit_rate": retrieval_report["hit_rate"],
            "threshold": MIN_HIT_RATE_AT_5,
            "top_k": top_k,
        }

    failed = [n for n, c in checks.items() if not c["pass"]]
    failed_critical = [n for n in failed if checks[n]["severity"] == "critical"]
    failed_advisory = [n for n in failed if checks[n]["severity"] == "advisory"]
    verdict = {
        "usable_as_test_seed": not failed_critical,
        "failed_checks": failed_critical,
        "advisories": failed_advisory,
        "notes": [],
    }
    if "template_diversity" in failed_advisory:
        verdict["notes"].append(
            f"正文高度模板化：掩掉部门/模块/主题与数字后仅 {len(skeletons)} 种骨架，"
            f"最常见骨架占 {top_skeleton_share:.1%} 的 chunk。"
            "这是合成种子的既定性质（同类场景共用句式，以主题/模块/数值区分），"
            "作为检索/权限管道的测试输入可接受，但不得当作真实制度文本。")
    if "retrieval_hit_rate" in failed_advisory:
        verdict["notes"].append(
            f"检索覆盖偏低：评测问题 hit@{top_k} = {retrieval_report['hit_rate']:.1%}"
            f"（参考下限 {MIN_HIT_RATE_AT_5:.0%}）。根因：正文不含部门名，而检索只索引"
            " heading_path+text（不含 title），故“在<部门>中…”类问题无法用部门词区分。"
            "本审计只报告不修改检索代码；如需提升，建议在重建语料时把部门/模块名写入正文"
            "（会改动内容，需另行评审）。")
    if len(docs) != total_db_docs:
        verdict["notes"].append(
            f"库内文档总数 {total_db_docs} > 种子 {len(docs)}：另有非种子条目"
            f"（status 分布 {by_status}），审计与测试均按 source={source} 计数。")

    return {
        "audit_version": "2.0",
        "db": str(db_path),
        "source": source,
        "counts": {
            "seed_documents": len(docs),
            "seed_chunks": len(chunks),
            "database_documents_total": total_db_docs,
            "database_status_distribution": by_status,
        },
        "coverage": {
            "by_department": dict(by_dept),
            "by_category": dict(by_category),
            "by_classification": dict(by_class),
        },
        "content_contract": {
            "authorization_fields": list(AUTH_FIELDS),
            "applicability_fields": list(APPLICABILITY_FIELDS),
            "contract_fields": list(CONTRACT_KEYS),
            "by_owner_department": dict(by_owner_department),
            "by_business_category": dict(by_business_category),
            "by_access_scope": dict(by_access_scope),
            "applicable_department_index_rows": applicable_index_rows,
            "documents_with_multi_applicable_departments": multi_select_docs,
            "legacy_department_mapping": {
                k: {"owner_department": v["owner_department"],
                    "business_category": v["business_category"],
                    "applicable_departments": list(v["applicable_departments"])}
                for k, v in LEGACY_DEPARTMENT_CONTRACT.items()
            },
            "finance_compliance_rule": {
                **FINANCE_COMPLIANCE_RULE,
                "applicable_departments": list(
                    FINANCE_COMPLIANCE_RULE["applicable_departments"]),
            },
            "chunk_contract_missing": chunk_contract_missing,
            "graph_contract_missing": graph_contract_missing,
            "data_dictionary": build_data_dictionary(),
        },
        "duplication": {
            "exact_duplicate_title_groups": len(title_dups),
            "exact_duplicate_title_ids": dup_title_ids,
            "exact_duplicate_body_groups": len(body_dups),
            "exact_duplicate_body_ids": body_dup_ids,
            "distinct_template_skeletons": len(skeletons),
            "top_skeleton_share": round(top_skeleton_share, 4),
        },
        "length": {
            "min": lens[0] if lens else 0,
            "median": lens[len(lens) // 2] if lens else 0,
            "max": lens[-1] if lens else 0,
            "too_short_count": len(too_short),
            "threshold": MIN_BODY_CHARS,
        },
        "metadata": {
            "doc_metadata_missing": doc_meta_missing,
            "chunk_metadata_missing": chunk_meta_missing,
            "provenance_missing": provenance_missing,
            "chunk_provenance_missing": chunk_provenance_missing,
        },
        "permission": {
            "by_classification": dict(by_class),
            "bad_classification_ids": bad_classification[:sample_limit],
            "scope_mismatch_ids": scope_mismatch[:sample_limit],
        },
        "source_license": {
            "source": source,
            "origin_values": dict(provenance_values),
            "license_values": dict(license_values),
            "is_real_company_data": False,
            "third_party_content": False,
        },
        "wording": {
            "demo_word_chunks": len(demo_word_ids),
            "fiction_word_chunks": len(fiction_word_ids),
            "real_company_claim_chunks": len(real_claim_ids),
            "sensitive_missing_disclaimer": len(sensitive_without_disclaimer),
        },
        "retrieval": {
            "fts_index_complete": fts_index_complete,
            "fts_missing_sample": fts_missing,
            "fts_rows": n_fts,
            **retrieval_report,
        },
        "checks": checks,
        "problem_samples": {k: v for k, v in problems.items()},
        "operations": {
            "backup_command": BACKUP_COMMAND,
            "rebuild_commands": list(REBUILD_COMMANDS),
            "deletion_performed": False,
            "out_of_scope_findings": list(OUT_OF_SCOPE_FINDINGS),
        },
        "verdict": verdict,
    }


def render_markdown(rep: dict) -> str:
    c = rep["checks"]
    v = rep["verdict"]
    lines: list[str] = []
    lines.append("# 测试期种子知识审计报告\n")
    lines.append(f"- 数据库: `{rep['db']}`")
    lines.append(f"- 种子 source: `{rep['source']}`")
    lines.append(f"- 审计版本: {rep['audit_version']}\n")
    lines.append("## 结论\n")
    lines.append(f"- **是否可作为测试期种子知识**: "
                 f"{'是（全部关键检查通过）' if v['usable_as_test_seed'] else '否'}")
    if v["failed_checks"]:
        lines.append(f"- 未通过的关键检查: {', '.join(v['failed_checks'])}")
    if v.get("advisories"):
        lines.append(f"- 需记录的提示（不阻断）: {', '.join(v['advisories'])}")
    for n in v["notes"]:
        lines.append(f"- 说明: {n}")
    lines.append("")

    lines.append("## 1. 总量与覆盖\n")
    lines.append(f"- 种子文档 / chunk: {rep['counts']['seed_documents']} / "
                 f"{rep['counts']['seed_chunks']}")
    lines.append(f"- 库内文档总数: {rep['counts']['database_documents_total']} "
                 f"（status: {rep['counts']['database_status_distribution']}）")
    lines.append("\n按部门:\n")
    lines.append("| 部门 | 条目 |\n| --- | --- |")
    for dept, n in rep["coverage"]["by_department"].items():
        lines.append(f"| {dept} | {n} |")
    lines.append("\n按场景类别:\n")
    lines.append("| 场景 | 条目 |\n| --- | --- |")
    for cat, n in rep["coverage"]["by_category"].items():
        lines.append(f"| {cat} | {n} |")
    lines.append(f"\n按权限分类: {rep['coverage']['by_classification']}\n")

    lines.append("## 2. 标题/正文重复与模板化\n")
    d = rep["duplication"]
    lines.append(f"- 完全重复标题组: {d['exact_duplicate_title_groups']}")
    lines.append(f"- 完全重复正文组: {d['exact_duplicate_body_groups']}")
    lines.append(f"- 模板骨架种类（掩部门/模块/主题/数字）: "
                 f"{d['distinct_template_skeletons']}，"
                 f"最常见骨架占比: {d['top_skeleton_share']:.1%}")
    if d["exact_duplicate_title_ids"]:
        lines.append(f"- 标题重复样例: {d['exact_duplicate_title_ids']}")
    if d["exact_duplicate_body_ids"]:
        lines.append(f"- 正文重复样例: {d['exact_duplicate_body_ids']}")
    lines.append("")

    lines.append("## 3. 过短 / 长度分布\n")
    ln = rep["length"]
    lines.append(f"- 最短/中位/最长: {ln['min']} / {ln['median']} / {ln['max']} 字符")
    lines.append(f"- 过短（< {ln['threshold']}）: {ln['too_short_count']}\n")

    lines.append("## 4. 元数据与出处完整性\n")
    m = rep["metadata"]
    lines.append(f"- 文档级 META_FIELDS 缺失: {m['doc_metadata_missing']}")
    lines.append(f"- chunk 级继承元数据缺失: {m['chunk_metadata_missing']}")
    lines.append(f"- 文档级 provenance 缺失: {m['provenance_missing']}")
    lines.append(f"- chunk 级 provenance 缺失: {m['chunk_provenance_missing']}")
    sl = rep["source_license"]
    lines.append(f"- origin 取值: {sl['origin_values']}")
    lines.append(f"- license 取值: {sl['license_values']}")
    lines.append(f"- is_real_company_data: {sl['is_real_company_data']}，"
                 f"含第三方内容: {sl['third_party_content']}\n")

    lines.append("## 5. 权限标签\n")
    p = rep["permission"]
    lines.append(f"- 分类分布: {p['by_classification']}")
    lines.append(f"- 非法 classification: {len(p['bad_classification_ids'])}")
    lines.append(f"- 权限/项目域不一致: {len(p['scope_mismatch_ids'])}")
    if p["scope_mismatch_ids"]:
        lines.append(f"  - 样例: {p['scope_mismatch_ids']}")
    lines.append("")

    cc = rep.get("content_contract", {})
    if cc:
        lines.append("## 5b. 内容适用范围契约（授权无关）\n")
        lines.append(f"- 授权字段（唯一参与授权）: {cc['authorization_fields']}")
        lines.append(f"- 适用范围字段: {cc['applicability_fields']}")
        lines.append(f"- 维护归属分布（owner_department）: {cc['by_owner_department']}")
        lines.append(f"- 业务分类分布（business_category）: {cc['by_business_category']}")
        lines.append(f"- 授权范围键分布（access_scope）: {cc['by_access_scope']}")
        lines.append(f"- 适用部门规范化索引行: {cc['applicable_department_index_rows']}")
        lines.append(f"- 多选适用部门文档数: "
                     f"{cc['documents_with_multi_applicable_departments']}")
        lines.append(f"- chunk 契约缺失: {cc['chunk_contract_missing']}，"
                     f"图谱行契约缺失: {cc['graph_contract_missing']}")
        lines.append("\n旧 department 标签 → 新契约映射（可审阅）:\n")
        lines.append("| 旧标签(历史) | owner_department | business_category | "
                     "applicable_departments |\n| --- | --- | --- | --- |")
        for label, m in cc["legacy_department_mapping"].items():
            lines.append(f"| {label} | {m['owner_department']} | "
                         f"{m['business_category']} | "
                         f"{', '.join(m['applicable_departments'])} |")
        rule = cc["finance_compliance_rule"]
        lines.append(f"\n- **财务合规分类规则**: {rule['rule']}")
        lines.append(f"  - is_department={rule['is_department']}，"
                     f"is_permission={rule['is_permission']}，"
                     f"business_category={rule['business_category']}，"
                     f"owner_department={rule['owner_department']}，"
                     f"applicable_departments={rule['applicable_departments']}")
        rmc = cc["data_dictionary"]["real_material_ingest_contract"]
        lines.append(f"\n- **真实资料入库契约**（origin ∈ {rmc['allowed_origins']}）必填: "
                     f"{rmc['required_fields']}")
        lines.append("")

    lines.append("## 6. 文案扫描（演示/虚构/真实公司声明）\n")
    w = rep["wording"]
    lines.append(f"- 含“演示/虚构/样例数据”字样 chunk: {w['demo_word_chunks']} / "
                 f"{w['fiction_word_chunks']}")
    lines.append(f"- 含疑似真实公司声明 chunk: {w['real_company_claim_chunks']}")
    lines.append(f"- 敏感条目缺“非真实数据”免责声明: "
                 f"{w['sensitive_missing_disclaimer']}\n")

    lines.append("## 7. 检索覆盖\n")
    r = rep["retrieval"]
    lines.append(f"- FTS 索引完整: {r['fts_index_complete']}（行数 {r['fts_rows']}）")
    if r.get("available"):
        lines.append(f"- 评测问题: {r['questions']} 条，hit@{r['top_k']} = "
                     f"{r['hit_rate']:.2%}，MRR = {r['mrr']:.4f}")
        lines.append("\n按部门:\n")
        lines.append("| 部门 | 样本 | 命中 | hit率 |\n| --- | --- | --- | --- |")
        for dept, b in r["by_department"].items():
            lines.append(f"| {dept or '-'} | {b['samples']} | {b['hits']} | "
                         f"{b['hit_rate']:.1%} |")
        if r["miss_samples"]:
            lines.append("\n未命中样例:\n")
            for s in r["miss_samples"]:
                lines.append(f"- {s['question']} → {s['gold_document_id']}")
    else:
        lines.append(f"- 检索覆盖: 跳过（{r.get('reason', '')}）")
    lines.append("")

    lines.append("## 8. 逐项检查\n")
    lines.append("| 检查 | 级别 | 结果 |\n| --- | --- | --- |")
    for name, chk in c.items():
        lines.append(f"| {name} | {chk.get('severity', 'critical')} | "
                     f"{'PASS' if chk['pass'] else 'FAIL'} |")
    lines.append("")

    if rep["problem_samples"]:
        lines.append("## 9. 问题 ID 抽样\n")
        for check, ids in rep["problem_samples"].items():
            lines.append(f"- `{check}`: {ids}")
        lines.append("")

    lines.append("## 10. 运维、备份与边界\n")
    lines.append(f"- 备份命令: `{BACKUP_COMMAND}`")
    lines.append("- 重建/检查命令:")
    for cmd in REBUILD_COMMANDS:
        lines.append(f"  ```bash\n  {cmd}\n  ```")
    lines.append("- 本次是否执行删除: 否（未删除任何原始数据；未使用 --force 重建）")
    lines.append("- 允许修改范围外、仍陈旧的文案（只报告，未修改）:\n")
    for f in OUT_OF_SCOPE_FINDINGS:
        lines.append(f"  - `{f['path']}` — {f['issue']}")
    lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(_PROJECT_ROOT / "data" / "knowledge.db"))
    parser.add_argument("--source", default=SEED_SOURCE)
    parser.add_argument("--json-out",
                        default=str(_PROJECT_ROOT / "data" / "knowledge_seed_audit.json"))
    parser.add_argument("--md-out",
                        default=str(_PROJECT_ROOT / "data" / "knowledge_seed_audit.md"))
    parser.add_argument(
        "--dictionary-out",
        default=str(_PROJECT_ROOT / "data" / "knowledge_metadata_dictionary.json"),
        help="元数据契约数据字典输出路径")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-retrieval", action="store_true",
                        help="跳过检索覆盖（不复制数据库）")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"错误: 数据库不存在: {db}", file=sys.stderr)
        return 2

    report = audit(db, source=args.source, sample_limit=args.sample_limit,
                   top_k=args.top_k, retrieval=not args.no_retrieval)

    jout = Path(args.json_out)
    jout.parent.mkdir(parents=True, exist_ok=True)
    jout.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    mout = Path(args.md_out)
    mout.parent.mkdir(parents=True, exist_ok=True)
    mout.write_text(render_markdown(report), encoding="utf-8")
    dout = Path(args.dictionary_out)
    dout.parent.mkdir(parents=True, exist_ok=True)
    dout.write_text(json.dumps(
        report["content_contract"]["data_dictionary"],
        ensure_ascii=False, indent=2), encoding="utf-8")

    v = report["verdict"]
    print(f"审计完成: 种子 {report['counts']['seed_documents']} 文档 / "
          f"{report['counts']['seed_chunks']} chunk")
    print(f"可作为测试期种子知识: {v['usable_as_test_seed']}")
    if v["failed_checks"]:
        print(f"未通过关键检查: {v['failed_checks']}")
    if v.get("advisories"):
        print(f"提示（不阻断）: {v['advisories']}")
    for n in v["notes"]:
        print(f"说明: {n}")
    print(f"报告: {jout} ; {mout}")
    print(f"数据字典: {dout}")
    return 0 if v["usable_as_test_seed"] else 1


if __name__ == "__main__":
    sys.exit(main())
