#!/usr/bin/env python
"""可重复运行的检索资产构建脚本 — 由当前 data/knowledge.db 再生成向量/图谱索引与报告.

产出（全部可由 knowledge.db + 本地模型再生成，不引入任何旧论文资产）：
  - data/vector/chunks_bge_m3/{meta.json,index.faiss}  — BGE-M3 稠密向量索引
    （默认用 sentence-transformers 离线加载缓存 BAAI/bge-m3；模型/库缺失时
    安全跳过并把向量组件如实标记 disabled，不影响 FTS）
  - data/vector/build_report.json — 向量构建报告（条数/维度/指纹/耗时/状态）
  - data/knowledge_graph_report.json — 图谱汇总报告（节点/边计数，按类型/谓词）

安全重建命令：
  .venv/bin/python scripts/build_knowledge_indices.py --db data/knowledge.db

图谱表在图数据构建时写入 knowledge.db（scripts/build_demo_knowledge.py）；
本脚本对图谱只做只读汇总报告。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_PROJECT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def load_index_rows(db_path: str | Path, conn):
    """从库中取用于建索引的 chunk 行（排除 archived；与 FTS 检索规则一致）。"""
    rows = conn.execute(
        "SELECT c.chunk_id, c.text, c.heading_path, c.page, c.document_id, "
        "c.version, c.status, c.metadata_json, "
        "d.title, d.department, d.classification, d.project_id, "
        "d.business_domain, d.metadata_json AS doc_meta_json "
        "FROM chunks c JOIN documents d ON d.document_id = c.document_id "
        "WHERE c.status != 'archived' AND d.status != 'archived' "
        "ORDER BY c.document_id, c.seq"
    ).fetchall()
    return rows


def build_vector(args) -> dict:
    from knowledge.metadata import finalize_meta
    from knowledge.vector_index import VectorIndex, resolve_embedder

    t0 = time.monotonic()
    db = Path(args.db)
    conn = __import__("sqlite3").connect(db)
    conn.row_factory = __import__("sqlite3").Row
    try:
        try:
            embedder = resolve_embedder(args.model, backend=args.backend,
                                        device=args.device)
        except Exception as e:
            return {
                "status": "skipped", "component": "vector",
                "error": f"{type(e).__name__}: {e}",
                "reason": "embedding 模型不可用；FTS 检索不受影响",
                "elapsed_s": round(time.monotonic() - t0, 2),
            }
        items = []
        for r in load_index_rows(db, conn):
            chunk_meta = __import__("json").loads(r["metadata_json"] or "{}")
            doc_meta = __import__("json").loads(r["doc_meta_json"] or "{}")
            meta = finalize_meta(
                {**doc_meta, **chunk_meta},
                department=r["department"], classification=r["classification"],
                project_id=r["project_id"], business_domain=r["business_domain"],
                status=r["status"],
            )
            items.append({
                "chunk_id": r["chunk_id"],
                "text": f"{r['heading_path']}\n{r['text']}",
                "heading_path": r["heading_path"],
                "page": r["page"],
                "document_id": r["document_id"],
                "title": r["title"],
                "version": r["version"],
                "status": r["status"],
                "meta": meta,
            })
        idx = VectorIndex(embedder).build(items)
        out_dir = Path(args.output)
        idx.save(out_dir)
        return {
            "status": "ok", "component": "vector",
            "index_path": str(out_dir),
            "chunks_indexed": idx.size,
            "dim": idx._build_dim,
            "model": embedder.name,
            "embedder": embedder.fingerprint,
            "elapsed_s": round(time.monotonic() - t0, 2),
        }
    finally:
        conn.close()


def build_graph_report(db_path: str | Path) -> dict:
    import sqlite3 as s3
    from knowledge.graphrag_pipeline import build_graph_report as _gbr
    conn = s3.connect(db_path)
    conn.row_factory = s3.Row
    try:
        return _gbr(conn)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建/校验检索资产（向量索引 + 图谱报告）")
    parser.add_argument("--db", default=str(_PROJECT_ROOT / "data" / "knowledge.db"))
    parser.add_argument("--output", default=str(_PROJECT_ROOT / "data" / "vector" / "chunks_bge_m3"))
    parser.add_argument("--model", default="BAAI/bge-m3",
                        help="本地 embedding 模型目录名/路径（离线缓存）")
    parser.add_argument("--backend", default="auto",
                        choices=("auto", "sentence_transformers", "flagembedding", "fake"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--vector-report", default=str(_PROJECT_ROOT / "data" / "vector" / "build_report.json"))
    parser.add_argument("--graph-report", default=str(_PROJECT_ROOT / "data" / "knowledge_graph_report.json"))
    parser.add_argument("--skip-vector", action="store_true")
    args = parser.parse_args(argv)

    if args.skip_vector:
        vector_report = {"status": "skipped", "component": "vector",
                         "reason": "--skip-vector 显式跳过"}
    else:
        vector_report = build_vector(args)

    graph_report = build_graph_report(args.db)

    vector_path = Path(args.vector_report)
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    vector_path.write_text(json.dumps(vector_report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    graph_path = Path(args.graph_report)
    graph_path.write_text(json.dumps(graph_report, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    print(json.dumps({
        "vector": vector_report,
        "graph": graph_report,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
