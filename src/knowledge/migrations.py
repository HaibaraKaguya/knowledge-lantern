"""Knowledge DB 授权元数据迁移 — 幂等、只增、事务保护.

目标: documents 表携带检索前所需的最小确定性授权元数据:

    classification  TEXT NOT NULL DEFAULT 'public'   -- public < internal < restricted
    access_scope    TEXT NOT NULL DEFAULT 'company_public'  -- 授权范围键（授权专用）
    project_id      TEXT                              -- 所属项目; NULL = 无项目限定
    business_domain TEXT NOT NULL DEFAULT 'research'  -- 业务域（research/finance/...）
    status          TEXT NOT NULL DEFAULT 'active'    -- active | draft | archived（已有列）

设计约束（与 src/access 检索过滤契约对齐）:
  - 授权元数据只存在 documents 表（单一事实源）。chunks 不复制这些可变策略字段，
    避免块级出现不一致副本；块检索通过 JOIN documents 继承文档级授权元数据。
  - 迁移只做 ADD COLUMN / CREATE INDEX / CREATE TABLE，绝不 DROP 列、绝不重写内容。
  - 幂等: 重复执行是 no-op，并报告 0 变更。

内容适用范围契约（与授权分离；migrate_content_contract / backfill_content_contract）:
  一度被混用的 `department` 收敛为独立的三层：
    适用范围   applicable_departments（多选列表，JSON 列 + 规范化索引表）
               / owner_department（维护归属）/ business_category（业务/场景分类）
    授权       classification / access_scope / project_id / business_domain / status
    `department` 保留为历史/兼容字段（不删除、不改值），新数据必须写新字段。
  索引结构（供后端在检索前按业务按钮过滤，不改变检索运行代码）：
    documents.applicable_departments_json  -- JSON 数组，可读结构
    document_applicable_departments(document_id, department_key)  -- 可索引过滤

检索面向的记录形状（retrieval-facing record shape）:
块检索结果必须暴露以下授权元数据，由 knowledge.models.SearchResult.authorization()
提供；检索智能体 / 后端按此形状消费，不得改从 chunk 推断:

    {
        "document_id": str,          # 出处主键
        "classification": str,       # "public" | "internal" | "restricted"
        "access_scope": str,         # 授权范围键（'company_public' 或受控范围键）
        "project_id": str | None,    # None = 无项目限定（语料 / 全局）
        "business_domain": str,      # 如 "research" / "finance"
        "status": str,               # "active" | "draft" | "archived"（检索默认排除 archived）
    }

公开研究语料回填:
  当前 50 篇公开 RAG/GraphRAG 论文全部来自 arXiv，按
  PUBLIC_CORPUS_WHERE 识别。回填为 classification='public'、
  project_id=NULL、business_domain='research'、status='active'。
  不宣称这些论文是企业的受限（restricted）数据。

测试期种子知识出处回填（backfill_seed_provenance）:
  1100 条测试期种子知识（source=SEED_SOURCE）在 metadata_json 中补上
  PROVENANCE_FIELDS：origin=seeded_synthetic_or_curated、
  is_real_company_data=False、license=项目自建无第三方内容、seed_label=测试期种子知识。
  只增键、不动内容/授权列/检索 ID，幂等；绝不把这些条目伪装成真实公司数据。
"""

from __future__ import annotations

import json
import sqlite3

from .models import (
    APPLICABILITY_FIELDS, LEGACY_DEPARTMENT_CONTRACT, PROVENANCE_FIELDS,
    SEED_PROVENANCE, derive_access_scope, normalize_applicable_departments,
)

# 授权元数据列（顺序即文档过滤契约顺序）
AUTH_COLUMNS = {
    "classification": "TEXT NOT NULL DEFAULT 'public'",
    "access_scope": "TEXT NOT NULL DEFAULT 'company_public'",
    "project_id": "TEXT",
    "business_domain": "TEXT NOT NULL DEFAULT 'research'",
    "submitter_id": "TEXT NOT NULL DEFAULT ''",
    "reviewer_id": "TEXT NOT NULL DEFAULT ''",
    "submitted_at": "TEXT NOT NULL DEFAULT ''",
    "reviewed_at": "TEXT NOT NULL DEFAULT ''",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "content_hash": "TEXT NOT NULL DEFAULT ''",
    "approval_note": "TEXT NOT NULL DEFAULT ''",
}

# ── 内容适用范围列（授权无关；新契约）─────────────────────────────────
# applicable_departments 以 JSON 数组列保存可读结构，同时经规范化索引表提供
# 可索引的等值过滤（见 CONTENT_CONTRACT_TABLE_DDL）。
CONTENT_CONTRACT_COLUMNS = {
    "owner_department": "TEXT NOT NULL DEFAULT ''",
    "business_category": "TEXT NOT NULL DEFAULT ''",
    "applicable_departments_json": "TEXT NOT NULL DEFAULT '[]'",
}

#: 规范化适用部门索引表（供后端在检索前按部门键过滤，不改检索运行代码）。
CONTENT_CONTRACT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS document_applicable_departments (
    document_id    TEXT NOT NULL,
    department_key TEXT NOT NULL,
    PRIMARY KEY (document_id, department_key)
);
CREATE INDEX IF NOT EXISTS idx_applicable_dept_key
ON document_applicable_departments(department_key);
"""

# ── 阶段 1：chunk 继承元数据 + 图谱表 ────────────────────────────────
# 图谱节点/边把 META_FIELDS 快照放进 meta_json；授权判定永远以 documents 表
# 当前列（JOIN）为单一事实源，meta_json 仅作描述性展示/离线检查。
GRAPH_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    node_type    TEXT NOT NULL,
    document_id  TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    meta_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_doc  ON graph_nodes(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(name, node_type);
CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id            TEXT PRIMARY KEY,
    document_id        TEXT NOT NULL,
    source_node_id     TEXT NOT NULL,
    target_node_id     TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    evidence_chunk_id  TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    meta_json          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_doc  ON graph_edges(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_src  ON graph_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_pred ON graph_edges(predicate);
"""

# 只增索引：
#   - idx_documents_active: 默认过滤 active 文档（status != 'archived'）
#   - idx_documents_auth:   按 classification/business_domain/project_id 过滤
# 两个索引覆盖现有检索与 src/access 过滤路径，不过度建索引。
# 索引以 (名称, DDL) 声明，名称由代码持有，避免从 DDL 正则反推。
ACTIVE_INDEX = ("idx_documents_active",
                "CREATE INDEX IF NOT EXISTS idx_documents_active "
                "ON documents(status) WHERE status != 'archived'")
AUTH_INDEX = ("idx_documents_auth",
              "CREATE INDEX IF NOT EXISTS idx_documents_auth "
              "ON documents(classification, business_domain, project_id)")
AUTH_INDEXES = (ACTIVE_INDEX, AUTH_INDEX)

# 公开研究语料识别谓词（与当前 50 篇 arxiv 论文精确匹配）
PUBLIC_CORPUS_WHERE = "source LIKE 'https://arxiv.org/%'"

#: 测试期种子知识的稳定血缘键（source 列）。该值被检索过滤无关的既有测试与
#: 报告引用，保持稳定；种子属性改由 metadata 的 PROVENANCE_FIELDS 表达。
SEED_SOURCE = "demo-knowledge-builder"

# 回填目标值
BACKFILL_TARGET = {
    "classification": "public",
    "project_id": None,
    "business_domain": "research",
    "status": "active",
}


class KnowledgeMigrationError(ValueError):
    """迁移/回填前置条件不满足或非法参数。"""


def _existing_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(documents)")}


def _existing_indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_documents_%'"
    ).fetchall()
    return {r[0] for r in rows}


def migrate_schema(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """幂等迁移 documents 授权元数据列与索引（只增不改）。

    参数:
      dry_run: True 时只计算将要添加的列/索引，不写数据库。
    返回:
      {"added_columns": [...], "added_indexes": [...]}
    若 documents 表不存在或缺少 document_id 主键 → KnowledgeMigrationError。
    """
    existing = _existing_columns(conn)
    if "document_id" not in existing:
        raise KnowledgeMigrationError(
            "documents 表不存在或缺少 document_id 主键；请先建库/导入语料再迁移"
        )

    added_columns: list[str] = []
    for name, ddl in AUTH_COLUMNS.items():
        if name not in existing:
            added_columns.append(name)
            if not dry_run:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")

    added_indexes: list[str] = []
    existing_idx = _existing_indexes(conn)
    for name, ddl in AUTH_INDEXES:
        if name not in existing_idx:
            added_indexes.append(name)
            if not dry_run:
                conn.execute(ddl)

    return {"added_columns": added_columns, "added_indexes": added_indexes}


def _backfill_where(scope: str, document_ids) -> tuple[str, list]:
    """把回填 scope 解析为 SQL WHERE + 绑定参数。"""
    if scope == "all":
        return "1=1", []
    if scope == "ids":
        ids = list(document_ids or [])
        if not ids:
            raise KnowledgeMigrationError("scope='ids' 需要至少一个 --document-id")
        ph = ",".join("?" * len(ids))
        return f"document_id IN ({ph})", ids
    if scope == "corpus":
        return PUBLIC_CORPUS_WHERE, []
    raise KnowledgeMigrationError(f"未知 scope: {scope}（可选: corpus/all/ids）")


def backfill_public_corpus(conn: sqlite3.Connection, *, scope: str = "corpus",
                           document_ids=None, dry_run: bool = False) -> dict:
    """把公开研究语料回填为 public/research/active/null-project。幂等。

    仅更新不满足目标值的行（WHERE NOT conforming），因此重复执行 updated=0。
    前置条件: 已执行 migrate_schema（授权列存在）。
    dry_run: 只统计，不 UPDATE。
    返回:
      {"scope", "total_in_scope", "conforming", "updated", "dry_run"}
    """
    missing = [c for c in AUTH_COLUMNS if c not in _existing_columns(conn)]
    if missing:
        raise KnowledgeMigrationError(
            f"缺少授权元数据列 {missing}；请先执行 migrate_schema"
        )

    where, params = _backfill_where(scope, document_ids)
    total = conn.execute(
        f"SELECT COUNT(*) FROM documents WHERE {where}", params
    ).fetchone()[0]
    conforming = conn.execute(
        f"SELECT COUNT(*) FROM documents WHERE {where} AND "
        "classification = 'public' AND project_id IS NULL "
        "AND business_domain = 'research' AND status = 'active'",
        params,
    ).fetchone()[0]
    would_update = total - conforming

    updated = 0
    if not dry_run and would_update:
        cur = conn.execute(
            f"UPDATE documents SET "
            "classification='public', project_id=NULL, "
            "business_domain='research', status='active' "
            f"WHERE {where} AND NOT "
            "(classification='public' AND project_id IS NULL "
            "AND business_domain='research' AND status='active')",
            params,
        )
        updated = cur.rowcount

    return {
        "scope": scope,
        "total_in_scope": total,
        "conforming": conforming,
        "updated": updated,
        "dry_run": dry_run,
    }


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def upgrade_retrieval_schema(conn: sqlite3.Connection,
                             *, dry_run: bool = False) -> dict:
    """幂等升级阶段 1 检索/图谱存储：chunk 继承元数据列 + 图谱表 + 回填。

    只做 ADD COLUMN / CREATE TABLE IF NOT EXISTS / UPDATE（幂等），不删不改既有列。
    documents 表不存在时跳过（无库可升）。返回:
      {"chunk_metadata_column": bool, "graph_tables_created": [...],
       "chunks_backfilled": int}
    """
    tables = _table_names(conn)
    result = {
        "chunk_metadata_column": False,
        "graph_tables_created": [],
        "chunks_backfilled": 0,
    }
    if "documents" not in tables:
        return result

    created = []
    for name in ("graph_nodes", "graph_edges"):
        if name not in tables:
            created.append(name)
    if created and not dry_run:
        conn.executescript(GRAPH_TABLE_DDL)

    if "chunks" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        if "metadata_json" not in cols:
            result["chunk_metadata_column"] = True
            if not dry_run:
                conn.execute(
                    "ALTER TABLE chunks ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
                )

    if not dry_run:
        # 回填：把旧库 chunk 的空白元数据复制为 documents 行现有 metadata_json
        # （新库在 ingest 时写入完整继承快照，此处仅让旧数据至少带上文档级描述）。
        # 描述字段最终展示始终与 documents 表 JOIN 合并，本快照非授权来源。
        cur = conn.execute(
            "UPDATE chunks SET metadata_json = "
            "(SELECT COALESCE(documents.metadata_json, '{}') FROM documents "
            " WHERE documents.document_id = chunks.document_id) "
            "WHERE metadata_json = '{}'"
        )
        result["chunks_backfilled"] = cur.rowcount
    return result


def _merge_contract_meta(metadata_json: str, target: dict) -> tuple[str, bool]:
    """把内容适用范围契约（+access_scope）合并进一条 metadata_json。

    只增/覆盖这组契约键，其余键原样保留；已符合目标时返回 changed=False（幂等）。
    """
    try:
        meta = json.loads(metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    incoming = {
        "access_scope": target["access_scope"],
        "applicable_departments": list(target["applicable_departments"]),
        "owner_department": target["owner_department"],
        "business_category": target["business_category"],
    }
    changed = any(meta.get(k) != v for k, v in incoming.items())
    if not changed:
        return json.dumps(meta, ensure_ascii=False), False
    meta.update(incoming)
    return json.dumps(meta, ensure_ascii=False), True


def migrate_content_contract(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """幂等补 documents 内容适用范围列 + 规范化适用部门索引表（只增不改）。

    只做 ADD COLUMN / CREATE TABLE / CREATE INDEX；表/列已存在时 no-op。
    返回: {"added_columns": [...], "applicable_index_table_created": bool}
    """
    existing = _existing_columns(conn)
    if "document_id" not in existing:
        raise KnowledgeMigrationError(
            "documents 表不存在或缺少 document_id 主键；请先建库/导入语料再迁移"
        )
    added_columns: list[str] = []
    for name, ddl in CONTENT_CONTRACT_COLUMNS.items():
        if name not in existing:
            added_columns.append(name)
            if not dry_run:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")

    table_created = "document_applicable_departments" not in _table_names(conn)
    if table_created and not dry_run:
        # 逐条 execute（不用 executescript：它会隐式 COMMIT，破坏调用方的事务）。
        for statement in CONTENT_CONTRACT_TABLE_DDL.split(";"):
            if statement.strip():
                conn.execute(statement)
    return {
        "added_columns": added_columns,
        "applicable_index_table_created": table_created,
    }


def content_contract_target(department: str, classification: str,
                            project_id, existing: dict, meta: dict) -> dict:
    """计算一条文档的目标内容适用范围契约（确定性、可审阅）。

    优先级：既有新字段（非空） > metadata 显式键 > 旧 department 标签映射 > 空。
    access_scope 始终由分类/项目派生，除非 metadata 给出显式值——保证授权范围
    与 classification/project_id 不漂移。
    """
    legacy = LEGACY_DEPARTMENT_CONTRACT.get(department or "", {})

    def _pick(existing_val, meta_key, legacy_key, default):
        if existing_val not in (None, "", "[]", []):
            return existing_val
        value = meta.get(meta_key)
        if value not in (None, "", [], ()):
            return value
        return legacy.get(legacy_key, default)

    try:
        existing_applicable = json.loads(
            existing.get("applicable_departments") or "[]")
    except (TypeError, json.JSONDecodeError):
        existing_applicable = []
    if not isinstance(existing_applicable, list):
        existing_applicable = []

    return {
        "owner_department": str(_pick(
            existing.get("owner_department"), "owner_department",
            "owner_department", "") or ""),
        "business_category": str(_pick(
            existing.get("business_category"), "business_category",
            "business_category", "") or ""),
        "applicable_departments": normalize_applicable_departments(_pick(
            existing_applicable, "applicable_departments",
            "applicable_departments", [])),
        # 授权范围键：metadata 显式值优先，否则由分类/项目派生（与授权列一致）。
        "access_scope": str(meta.get("access_scope")
                            or derive_access_scope(classification, project_id)),
    }


def backfill_content_contract(conn: sqlite3.Connection, *, source: str | None = None,
                              dry_run: bool = False) -> dict:
    """把旧 `department` 标签收敛为新契约：列 + metadata_json + chunk/图谱快照。

    只增/只改契约字段（access_scope/applicable_departments/owner_department/
    business_category），绝不改动内容、授权列(classification/project_id/...)、
    document_id/chunk_id 或 provenance；幂等（重复执行 0 变更）。
    source=None → 全部文档；source=SEED_SOURCE → 仅测试期种子知识。
    调用方负责事务提交（本函数不 commit）。

    返回计数：documents_in_scope/updated、chunks_in_scope/updated、
    graph_nodes_updated、graph_edges_updated、applicable_rows_written、dry_run。
    """
    cols = _existing_columns(conn)
    if "document_id" not in cols:
        raise KnowledgeMigrationError("documents 表不存在；请先建库/导入语料")
    missing = [c for c in CONTENT_CONTRACT_COLUMNS if c not in cols]
    if "access_scope" not in cols:
        missing.append("access_scope")
    if missing:
        raise KnowledgeMigrationError(
            f"缺少内容适用范围契约列 {missing}；"
            "请先执行 migrate_schema + migrate_content_contract")

    where, params = ("1=1", []) if source is None else ("source = ?", [source])
    rows = conn.execute(
        "SELECT document_id, department, classification, project_id, metadata_json, "
        "access_scope, owner_department, business_category, "
        "applicable_departments_json "
        f"FROM documents WHERE {where}",
        params,
    ).fetchall()

    doc_updates: list[tuple] = []
    meta_updates: list[tuple] = []
    updated_doc_ids: set[str] = set()
    apply_rows: list[tuple[str, str]] = []
    targets: dict[str, dict] = {}
    applicable_rows_written = 0

    for document_id, department, classification, project_id, metadata_json, \
            access_scope, owner_department, business_category, applicable_json in rows:
        try:
            meta = json.loads(metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        existing = {
            "owner_department": owner_department,
            "business_category": business_category,
            "applicable_departments": applicable_json,
        }
        target = content_contract_target(
            department or "", classification or "public", project_id, existing, meta)
        targets[document_id] = target
        applicable_rows_written += len(target["applicable_departments"])
        apply_rows.extend(
            (document_id, key) for key in target["applicable_departments"])

        new_applicable_json = json.dumps(
            target["applicable_departments"], ensure_ascii=False)
        if ((access_scope or "") != target["access_scope"]
                or owner_department != target["owner_department"]
                or business_category != target["business_category"]
                or (applicable_json or "[]") != new_applicable_json):
            doc_updates.append((
                target["access_scope"], target["owner_department"],
                target["business_category"], new_applicable_json, document_id))
            updated_doc_ids.add(document_id)
        new_meta_json, changed = _merge_contract_meta(metadata_json, target)
        if changed:
            meta_updates.append((new_meta_json, document_id))
            updated_doc_ids.add(document_id)

    chunk_in_scope = chunk_updates = 0
    graph_node_updates = graph_edge_updates = 0
    tables = _table_names(conn)
    if "chunks" in tables:
        chunk_where = "1=1" if source is None else "d.source = ?"
        chunk_rows = conn.execute(
            "SELECT c.chunk_id, c.document_id, c.metadata_json FROM chunks c "
            "JOIN documents d ON d.document_id = c.document_id "
            f"WHERE {chunk_where}",
            params,
        ).fetchall()
        chunk_in_scope = len(chunk_rows)
        chunk_meta_updates = []
        for chunk_id, document_id, metadata_json in chunk_rows:
            target = targets.get(document_id)
            if not target:
                continue
            new_json, changed = _merge_contract_meta(metadata_json, target)
            if changed:
                chunk_meta_updates.append((new_json, chunk_id))
        chunk_updates = len(chunk_meta_updates)
        if chunk_meta_updates and not dry_run:
            conn.executemany(
                "UPDATE chunks SET metadata_json = ? WHERE chunk_id = ?",
                chunk_meta_updates)

    for table, counter in (("graph_nodes", "nodes"), ("graph_edges", "edges")):
        if table not in tables:
            continue
        g_rows = conn.execute(
            f"SELECT rowid AS rid, document_id, meta_json FROM {table}").fetchall()
        g_updates = []
        for rid, document_id, meta_json in g_rows:
            target = targets.get(document_id)
            if not target:
                continue
            new_json, changed = _merge_contract_meta(meta_json, target)
            if changed:
                g_updates.append((new_json, rid))
        if table == "graph_nodes":
            graph_node_updates = len(g_updates)
        else:
            graph_edge_updates = len(g_updates)
        if g_updates and not dry_run:
            conn.executemany(
                f"UPDATE {table} SET meta_json = ? WHERE rowid = ?", g_updates)

    if not dry_run:
        if doc_updates:
            conn.executemany(
                "UPDATE documents SET access_scope = ?, owner_department = ?, "
                "business_category = ?, applicable_departments_json = ? "
                "WHERE document_id = ?",
                doc_updates)
        if meta_updates:
            conn.executemany(
                "UPDATE documents SET metadata_json = ? WHERE document_id = ?",
                meta_updates)
        doc_ids = [r[0] for r in rows]
        if doc_ids:
            ph = ",".join("?" * len(doc_ids))
            conn.execute(
                f"DELETE FROM document_applicable_departments "
                f"WHERE document_id IN ({ph})", doc_ids)
            if apply_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO document_applicable_departments "
                    "(document_id, department_key) VALUES (?, ?)", apply_rows)

    return {
        "scope": source or "all",
        "documents_in_scope": len(rows),
        "documents_updated": len(updated_doc_ids),
        "chunks_in_scope": chunk_in_scope,
        "chunks_updated": chunk_updates,
        "graph_nodes_updated": graph_node_updates,
        "graph_edges_updated": graph_edge_updates,
        "applicable_rows_written": applicable_rows_written,
        "dry_run": dry_run,
        "contract_fields": ["access_scope", *APPLICABILITY_FIELDS],
    }


def _merge_provenance(metadata_json: str) -> tuple[str, bool]:
    """把 SEED_PROVENANCE 合并进一条 metadata_json；返回 (新 JSON, 是否变更)。

    只增不删：已存在的其它键原样保留；已符合种子出处的行不变更（幂等）。
    """
    try:
        meta = json.loads(metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    changed = any(meta.get(k) != v for k, v in SEED_PROVENANCE.items())
    if not changed:
        return json.dumps(meta, ensure_ascii=False), False
    meta.update(SEED_PROVENANCE)
    return json.dumps(meta, ensure_ascii=False), True


def backfill_seed_provenance(conn: sqlite3.Connection, *,
                             source: str = SEED_SOURCE,
                             dry_run: bool = False) -> dict:
    """为种子知识回填 PROVENANCE_FIELDS（origin=seeded_synthetic_or_curated 等）。

    只增键、不改动既有内容/授权列/检索 ID，幂等（重复执行 updated=0）。
    同时更新 documents 与 chunks 的 metadata_json 快照，使“静止态”也带出处。
    调用方负责事务提交（与 migrate_schema 一致，本函数不 commit）。

    返回:
      {"source", "documents_in_scope", "documents_updated",
       "chunks_in_scope", "chunks_updated", "dry_run", "provenance": {...}}
    """
    cols = _existing_columns(conn)
    if "document_id" not in cols:
        raise KnowledgeMigrationError("documents 表不存在；请先建库/导入语料")
    if "metadata_json" not in cols:
        raise KnowledgeMigrationError("documents 表缺少 metadata_json 列")

    doc_rows = conn.execute(
        "SELECT document_id, metadata_json FROM documents WHERE source = ?",
        (source,),
    ).fetchall()
    doc_updates = []
    for document_id, metadata_json in doc_rows:
        new_json, changed = _merge_provenance(metadata_json)
        if changed:
            doc_updates.append((new_json, document_id))
    if doc_updates and not dry_run:
        conn.executemany(
            "UPDATE documents SET metadata_json = ? WHERE document_id = ?",
            doc_updates,
        )

    chunk_in_scope = 0
    chunk_updates = []
    if "chunks" in _table_names(conn):
        chunk_cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
        if "metadata_json" in chunk_cols:
            chunk_rows = conn.execute(
                "SELECT c.chunk_id, c.metadata_json FROM chunks c "
                "JOIN documents d ON d.document_id = c.document_id "
                "WHERE d.source = ?", (source,),
            ).fetchall()
            chunk_in_scope = len(chunk_rows)
            for chunk_id, metadata_json in chunk_rows:
                new_json, changed = _merge_provenance(metadata_json)
                if changed:
                    chunk_updates.append((new_json, chunk_id))
            if chunk_updates and not dry_run:
                conn.executemany(
                    "UPDATE chunks SET metadata_json = ? WHERE chunk_id = ?",
                    chunk_updates,
                )

    return {
        "source": source,
        "documents_in_scope": len(doc_rows),
        "documents_updated": len(doc_updates),
        "chunks_in_scope": chunk_in_scope,
        "chunks_updated": len(chunk_updates),
        "dry_run": dry_run,
        "provenance": dict(SEED_PROVENANCE),
        "provenance_fields": list(PROVENANCE_FIELDS),
    }


def run_migrations(db_path, *, dry_run: bool = False, backfill: bool = True,
                   scope: str = "corpus", document_ids=None,
                   seed_provenance: bool = False,
                   content_contract: bool = True) -> dict:
    """对单个 db 文件执行迁移 + 回填，返回汇总报告。

    - dry_run=True: 不动库。schema 变更只预览；回填计数在"迁移已应用"的
      回滚事务中计算，保证预览与真实结果一致。
    - dry_run=False: 单事务（BEGIN IMMEDIATE → COMMIT / ROLLBACK），
      迁移与回填原子提交，异常时整体回滚。
    - content_contract=True: 同时补内容适用范围列/索引表，并把旧 department
      标签回填为新契约（documents/chunk/图谱快照 + 规范化索引表）。
    """
    conn = sqlite3.connect(db_path)
    try:
        if dry_run:
            schema = migrate_schema(conn, dry_run=True)
            cc_schema = migrate_content_contract(conn, dry_run=True)
            bf = None
            prov = None
            cc = None
            if backfill:
                # 在回滚事务中应用迁移，让回填计数基于真实目标 schema。
                conn.execute("BEGIN")
                migrate_schema(conn, dry_run=False)
                migrate_content_contract(conn, dry_run=False)
                bf = backfill_public_corpus(
                    conn, scope=scope, document_ids=document_ids, dry_run=True
                )
                if seed_provenance:
                    prov = backfill_seed_provenance(conn, dry_run=True)
                if content_contract:
                    cc = backfill_content_contract(conn, dry_run=True)
                conn.rollback()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            return {
                "db": str(db_path), "dry_run": True,
                "schema": schema, "content_contract_schema": cc_schema,
                "backfill": bf, "seed_provenance": prov,
                "content_contract": cc, "integrity": integrity,
            }

        conn.execute("BEGIN IMMEDIATE")
        try:
            schema = migrate_schema(conn, dry_run=False)
            cc_schema = migrate_content_contract(conn, dry_run=False)
            bf = None
            if backfill:
                bf = backfill_public_corpus(
                    conn, scope=scope, document_ids=document_ids, dry_run=False
                )
            prov = None
            if seed_provenance:
                prov = backfill_seed_provenance(conn, dry_run=False)
            cc = None
            if content_contract:
                cc = backfill_content_contract(conn, dry_run=False)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "db": str(db_path), "dry_run": False,
            "schema": schema, "content_contract_schema": cc_schema,
            "backfill": bf, "seed_provenance": prov,
            "content_contract": cc, "integrity": integrity,
        }
    finally:
        conn.close()
