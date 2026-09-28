"""Knowledge 授权元数据迁移 CLI — 显式、安全、幂等.

用法:
  python scripts/migrate_knowledge_metadata.py                     # 迁移 + 语料回填 + 内容适用范围回填
  python scripts/migrate_knowledge_metadata.py --dry-run           # 仅预览，不改库
  python scripts/migrate_knowledge_metadata.py --no-backfill       # 只补列/索引，不回填
  python scripts/migrate_knowledge_metadata.py --scope all         # 回填全部文档
  python scripts/migrate_knowledge_metadata.py --scope ids --document-id X --document-id Y
  python scripts/migrate_knowledge_metadata.py --seed-provenance   # 补测试期种子知识出处
  python scripts/migrate_knowledge_metadata.py --no-content-contract  # 跳过适用范围契约回填

安全性:
  - 只增（ADD COLUMN / CREATE TABLE / CREATE INDEX / metadata_json 补键），绝不 DROP、不重写内容。
  - 迁移 + 回填在单事务内原子提交（BEGIN IMMEDIATE → COMMIT / ROLLBACK）。
  - 幂等: 重复执行是 no-op，报告 0 变更。
  - 回填只更新"不满足目标值"的行，并报告实际变更行数。

公开语料回填（--scope corpus，默认）:
  识别源为 arXiv 的公开研究语料（当前 50 篇 RAG/GraphRAG 论文），
  回填为 classification='public'、project_id=NULL、business_domain='research'、
  status='active'。不宣称这些论文是企业受限数据。

测试期种子知识出处（--seed-provenance）:
  为 1100 条测试期种子知识（source=demo-knowledge-builder）补 PROVENANCE_FIELDS：
  origin=seeded_synthetic_or_curated、is_real_company_data=False 等。只增键、幂等。

内容适用范围契约（默认执行，--no-content-contract 关闭）:
  把旧 `department` 标签收敛为新契约：documents 增列 access_scope /
  owner_department / business_category / applicable_departments_json，
  建规范化索引表 document_applicable_departments，并把新字段回填进
  documents / chunks / 图谱快照的 metadata_json（只增键、幂等、不动内容与授权列）。
  「财务合规」按其业务分类规则映射（business_category=finance_compliance、
  owner_department=finance、applicable_departments=[finance, business_analysis]），
  绝不作为权限字段替代。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowledge.migrations import (  # noqa: E402
    KnowledgeMigrationError,
    run_migrations,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/knowledge.db",
                        help="目标 SQLite 数据库（默认 data/knowledge.db）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只预览将执行的变更，不写数据库")
    parser.add_argument("--no-backfill", action="store_true",
                        help="只做 schema 迁移（补列/索引），跳过回填")
    parser.add_argument("--scope", choices=("corpus", "all", "ids"),
                        default="corpus",
                        help="回填范围: corpus=arXiv 公开研究语料(默认), "
                             "all=全部文档, ids=显式 document_id 列表")
    parser.add_argument("--document-id", action="append", default=[],
                        metavar="ID", help="scope=ids 时的文档 id（可重复）")
    parser.add_argument("--seed-provenance", action="store_true",
                        help="同时回填测试期种子知识出处字段（只增键、幂等）")
    parser.add_argument("--no-content-contract", action="store_true",
                        help="跳过内容适用范围契约列/索引/回填（默认执行）")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"错误: 数据库不存在: {args.db}", file=sys.stderr)
        return 2

    try:
        report = run_migrations(
            str(args.db),
            dry_run=args.dry_run,
            backfill=not args.no_backfill,
            scope=args.scope,
            document_ids=args.document_id or None,
            seed_provenance=args.seed_provenance,
            content_contract=not args.no_content_contract,
        )
    except (KnowledgeMigrationError, sqlite3.Error) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
