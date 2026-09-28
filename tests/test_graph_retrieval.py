"""图谱/元数据测试 — 图节点边继承元数据 + 无权角色无法经 chunk/node/edge 泄漏.

覆盖需求 3/4/6：
  3) 图节点/边继承完整必需元数据（department/classification/.../error_code）；
  4) unauthorized 角色无法经 chunk、node 或 edge 获得受限内容；
  6) 数据构建后五部门各约 220 chunk 且必需元数据存在。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for p in (_ROOT / "src", _ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import build_demo_knowledge  # noqa: E402
from knowledge.graph_retrieval import GraphReader  # noqa: E402
from knowledge.graphrag_pipeline import build_graph_report  # noqa: E402
from knowledge.metadata import inherit_metadata  # noqa: E402
from knowledge.models import META_FIELDS  # noqa: E402
from knowledge.store import KnowledgeStore  # noqa: E402
from access import (  # noqa: E402
    AccessContext,
    Action,
    Classification,
    build_retrieval_filter,
)
from paper_qa.retrieval import ReadOnlyPaperRetriever  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="graph-test-")
_DB = str(Path(_TMP) / "knowledge.db")


def setUpModule():
    build_demo_knowledge.build(_DB, force=True)


def tearDownModule():
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _ctx(user_id: str, ceiling: Classification,
         project_scopes=None, domain_scopes=None) -> AccessContext:
    return AccessContext(
        user_id=user_id, user_name=user_id, roles=(user_id,),
        allowed_actions=frozenset([Action.INTERNAL_RETRIEVAL_ASK]),
        project_scopes=(None if project_scopes is None
                        else frozenset(project_scopes)),
        business_domain_scopes=(None if domain_scopes is None
                                else frozenset(domain_scopes)),
        classification_ceiling=ceiling,
    )


EMPLOYEE = _ctx("employee", Classification.PUBLIC)
ACCOUNTANT = _ctx("accountant", Classification.INTERNAL,
                  project_scopes=["finance-ledger"], domain_scopes=["finance"])
SUPERVISOR = _ctx("finance_supervisor", Classification.RESTRICTED,
                  project_scopes=["finance-ledger"], domain_scopes=["finance"])


def _demo_docs(conn):
    rows = conn.execute(
        "SELECT document_id, department, classification, project_id, "
        "business_domain, status, title, metadata_json "
        "FROM documents WHERE source='demo-knowledge-builder'"
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        out.append({**{k: r[k] for k in r.keys()}, "meta": meta})
    return out


class TestBuildMetadataAndGraph(unittest.TestCase):
    """需求 6：五部门各 ~220 chunk + 必需元数据 + 图节点/边存在且继承元数据。"""

    def test_five_depts_220_chunks_with_required_meta(self):
        conn = _connect_db()
        try:
            by_dept = dict(conn.execute(
                "SELECT department, COUNT(*) FROM documents "
                "WHERE source='demo-knowledge-builder' GROUP BY department"
            ).fetchall())
            self.assertEqual(len(by_dept), 5)
            for dept, n in by_dept.items():
                self.assertEqual(n, 220, dept)
            # 每部门 220 chunk 且必需元数据齐全
            for dept in by_dept:
                n_chunks = conn.execute(
                    "SELECT COUNT(*) FROM chunks c JOIN documents d "
                    "ON d.document_id = c.document_id "
                    "WHERE d.department = ? AND d.source='demo-knowledge-builder' "
                    "AND c.metadata_json != '{}'",
                    (dept,),
                ).fetchone()[0]
                self.assertEqual(n_chunks, 220, dept)
            missing = conn.execute(
                "SELECT COUNT(*) FROM chunks c JOIN documents d "
                "ON d.document_id = c.document_id "
                "WHERE d.source='demo-knowledge-builder' AND c.metadata_json='{}'"
            ).fetchone()[0]
            self.assertEqual(missing, 0)
            # 字段完整
            meta = json.loads(conn.execute(
                "SELECT c.metadata_json FROM chunks c JOIN documents d "
                "ON d.document_id=c.document_id WHERE d.source='demo-knowledge-builder' "
                "LIMIT 1").fetchone()[0])
            for key in META_FIELDS:
                self.assertIn(key, meta, f"chunk metadata 缺 {key}")
        finally:
            conn.close()

    def test_graph_rows_exist_and_report_shape(self):
        conn = _connect_db()
        try:
            report = build_graph_report(conn)
            self.assertGreater(report["nodes"], 0)
            self.assertGreater(report["edges"], 0)
            self.assertTrue(report["enabled"])
            self.assertIn("module", report["nodes_by_type"])
            self.assertIn("error_code", report["nodes_by_type"])
            self.assertIn("cause", report["edges_by_predicate"])
            self.assertIn("solution", report["edges_by_predicate"])
            self.assertIn("dependency", report["edges_by_predicate"])
            self.assertIn("precondition", report["edges_by_predicate"])
            self.assertIn("applies_to", report["edges_by_predicate"])
        finally:
            conn.close()

    def test_nodes_and_edges_inherit_required_meta(self):
        conn = _connect_db()
        try:
            docs = {d["document_id"]: d for d in _demo_docs(conn)}
            # 每类节点抽样：module / error_code / role 的名称应等于其继承 meta 的
            # 对应字段（module / error_code / applicable_role），且授权列=文档当前列。
            for ntype, metakey in (("module", "module"), ("error_code", "error_code"),
                                   ("role", "applicable_role")):
                row = conn.execute(
                    "SELECT g.name, g.node_type, g.meta_json, g.document_id "
                    "FROM graph_nodes g "
                    "WHERE g.node_type=? AND g.name != '' LIMIT 1", (ntype,)
                ).fetchone()
                self.assertIsNotNone(row, ntype)
                meta = json.loads(row["meta_json"])
                doc = docs[row["document_id"]]
                self.assertEqual(meta[metakey], row["name"],
                                 f"{ntype} 节点应继承自身 {metakey} 元数据")
                self.assertEqual(meta["department"], doc["department"])
                self.assertEqual(meta["classification"], doc["classification"])
                self.assertEqual(meta["project_id"], doc["project_id"])
                self.assertEqual(meta["business_domain"], doc["business_domain"])
            # 边继承元数据（含谓词对应描述）
            e = conn.execute(
                "SELECT e.meta_json, e.document_id FROM graph_edges e "
                "WHERE e.predicate='cause' LIMIT 1").fetchone()
            self.assertIsNotNone(e)
            emeta = json.loads(e["meta_json"])
            self.assertEqual(emeta["document_type"], "troubleshooting")
            self.assertEqual(emeta["department"], docs[e["document_id"]]["department"])
            self.assertIsInstance(emeta["demo"], bool)
        finally:
            conn.close()


def _restricted_fc_doc(conn) -> dict:
    """挑一篇财务合规的 restricted 演示文档（含 module/document_type 元数据）。"""
    row = conn.execute(
        "SELECT document_id, title, metadata_json FROM documents "
        "WHERE classification='restricted' AND source='demo-knowledge-builder' "
        "AND department='财务合规' LIMIT 1"
    ).fetchone()
    meta = json.loads(row["metadata_json"])
    return {"document_id": row["document_id"], "title": row["title"],
            "module": meta["module"], "document_type": meta["document_type"]}


class TestGraphAccess(unittest.TestCase):
    """需求 4：unauthorized 角色无法经 node/edge/chunk 获得受限内容。"""

    def _expansion(self, query: str, ctx, top_k: int = 10):
        conn = _connect_db()
        try:
            filt = build_retrieval_filter(ctx)
            return GraphReader(conn).expand(query, top_k=top_k,
                                            access_filter=filt,
                                            access_context=ctx)
        finally:
            conn.close()

    def test_restricted_doc_invisible_to_employee_and_accountant(self):
        conn = _connect_db()
        try:
            doc = _restricted_fc_doc(conn)
        finally:
            conn.close()
        for ctx in (EMPLOYEE, ACCOUNTANT):
            exp = self._expansion(doc["title"], ctx)
            self.assertNotIn(doc["document_id"], exp.matched_documents,
                             f"{ctx.user_id} 不应扩展受限文档")
            self.assertFalse(any(h.document_id == doc["document_id"]
                                 for h in exp.node_hits),
                             f"{ctx.user_id} 不应看到受限节点")
            self.assertFalse(any(e.document_id == doc["document_id"]
                                 for e in exp.edge_hits),
                             f"{ctx.user_id} 不应看到受限边")
            self.assertFalse(any(cid.startswith(doc["document_id"] + ":")
                                 for cid in exp.expanded_chunk_ids),
                             f"{ctx.user_id} 不应拿到受限 chunk")

    def test_restricted_doc_visible_to_supervisor(self):
        conn = _connect_db()
        try:
            doc = _restricted_fc_doc(conn)
        finally:
            conn.close()
        exp = self._expansion(doc["title"], SUPERVISOR, top_k=10)
        self.assertIn(doc["document_id"], exp.matched_documents,
                      "supervisor 应能看到受限文档参与图谱扩展")
        self.assertGreater(exp.hits, 0)
        self.assertTrue(any(e.document_id == doc["document_id"] for e in exp.edge_hits)
                        or any(n.document_id == doc["document_id"] for n in exp.node_hits),
                        "supervisor 的扩展应含受限文档的节点或边")

    def test_graph_nodes_and_edges_never_from_restricted_doc_for_employee(self):
        """employee 扩展命中来自公开文档的节点/边，绝无受限文档来源。"""
        conn = _connect_db()
        try:
            doc = _restricted_fc_doc(conn)
        finally:
            conn.close()
        exp = self._expansion(doc["module"], EMPLOYEE, top_k=20)
        for h in exp.node_hits:
            self.assertNotEqual(h.document_id, doc["document_id"])
            self.assertEqual(h.meta["classification"], "public",
                             "employee 的图节点只能来自公开文档")
        for e in exp.edge_hits:
            self.assertNotEqual(e.document_id, doc["document_id"])

    def test_chunk_path_no_leak(self):
        conn = _connect_db()
        try:
            doc = _restricted_fc_doc(conn)
        finally:
            conn.close()
        r = ReadOnlyPaperRetriever(_DB, graph_enabled=False,
                                   vector_enabled=False)
        res = r.search(doc["title"], top_k=10,
                       access_filter=build_retrieval_filter(ACCOUNTANT),
                       access_context=ACCOUNTANT)
        self.assertTrue(all(c.document_id != doc["document_id"] for c in res.chunks),
                        "accountant 检索不应返回 restricted 文档 chunk")
        res2 = r.search(doc["title"], top_k=10,
                        access_filter=build_retrieval_filter(SUPERVISOR),
                        access_context=SUPERVISOR)
        self.assertTrue(any(c.document_id == doc["document_id"] for c in res2.chunks),
                        "supervisor 应能检索到 restricted 文档 chunk")


if __name__ == "__main__":
    unittest.main()
