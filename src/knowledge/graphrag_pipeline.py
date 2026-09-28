"""GraphRAG 管线 — 面向"部门知识库检索扩展"的图阶段封装.

本阶段 GraphRAG 的职责边界：
  - 不替代 BM25/向量文本检索；它提供实体/关系/元数据的检索扩展候选与
    结构化图证据（命中的节点/边/来源），与文本分支做加权 RRF 融合；
  - 所有图检索输出（节点、边、扩展 chunk、文档）先执行访问过滤
    （粗过滤 JOIN documents 授权列 + 逐条 decide()），绝不泄漏无权内容；
  - 图数据由 data 构建（scripts/build_demo_knowledge.py 在 ingest 时写入
    graph_nodes / graph_edges），本管线只做只读读取与状态报告。

组件状态契约（health/status 展示用）：
  {"enabled": bool, "nodes": int, "edges": int, "error": str|None}
  图缺失/损坏/为空 → enabled=False + error 原因，绝不报告 graph 已开启。
"""

from __future__ import annotations

import sqlite3

from .graph_retrieval import (
    GraphEdgeHit,
    GraphExpansion,
    GraphNodeHit,
    GraphReader,
)


class GraphPipeline:
    """只读图谱管线（连接生命周期由调用方管理，通常每次检索一条只读连接）。"""

    def __init__(self, conn: sqlite3.Connection):
        self._reader = GraphReader(conn)

    # ── 组件状态 ───────────────────────────────────────────

    def status(self) -> dict:
        """图谱组件状态（不含文本检索状态）。"""
        return self._reader.status()

    def expand(self, query: str, *, top_k: int = 10,
               access_filter=None, access_context=None,
               max_candidates: int = 40) -> GraphExpansion:
        """访问过滤后的图谱扩展候选。"""
        return self._reader.expand(
            query, top_k=top_k, access_filter=access_filter,
            access_context=access_context, max_candidates=max_candidates,
        )


def graph_stats(conn: sqlite3.Connection) -> dict:
    """便捷状态统计（供构建脚本报告/重建校验）。"""
    return GraphPipeline(conn).status()


def build_graph_report(conn: sqlite3.Connection) -> dict:
    """构建脚本用的图谱汇总报告。"""
    try:
        status = graph_stats(conn)
    except sqlite3.OperationalError as e:
        return {"nodes": 0, "edges": 0, "error": str(e)}
    try:
        by_type = {r[0]: r[1] for r in conn.execute(
            "SELECT node_type, COUNT(*) FROM graph_nodes GROUP BY node_type")}
        by_pred = {r[0]: r[1] for r in conn.execute(
            "SELECT predicate, COUNT(*) FROM graph_edges GROUP BY predicate")}
    except sqlite3.OperationalError:
        by_type, by_pred = {}, {}
    return {
        "nodes": status.get("nodes", 0),
        "edges": status.get("edges", 0),
        "enabled": status.get("enabled", False),
        "error": status.get("error"),
        "nodes_by_type": by_type,
        "edges_by_predicate": by_pred,
    }


__all__ = [
    "GraphPipeline",
    "graph_stats",
    "build_graph_report",
    "GraphExpansion",
    "GraphNodeHit",
    "GraphEdgeHit",
]
