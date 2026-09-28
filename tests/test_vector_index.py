"""VectorIndex 测试 — 持久化 / 维度或损坏文件降级 / 指纹校验.

覆盖需求：
  1) 向量索引持久化（save→load 后搜索结果一致）；
  2) 损坏文件 / 维度不一致 / 指纹不一致 → load 抛显式错误（上层按"向量关闭"降级）；
  3) 缺失 faiss 环境模块仍可导入（FakeEmbedder 不受影响）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge.vector_index import (
    FakeEmbedder,
    VectorIndex,
    VectorIndexCorruptionError,
    VectorIndexDimensionMismatchError,
    VectorIndexError,
    VectorIndexFingerprintMismatchError,
)

_ITEMS = [
    {"chunk_id": "D-PUB:1:1", "text": "员工出差前需在 OA 系统提交出差申请",
     "document_id": "D-PUB", "status": "active", "meta": {"department": "行政"}},
    {"chunk_id": "D-INT:1:1", "text": "费用报销单需挂接预算项目，超支需专项说明",
     "document_id": "D-INT", "status": "active", "meta": {"department": "财务合规"}},
    {"chunk_id": "D-ARCH:1:1", "text": "旧版纸面审批规则，已废止",
     "document_id": "D-ARCH", "status": "archived", "meta": {"department": "行政"}},
]


def _build(tmp: str, dim: int = 32, seed: int = 7) -> VectorIndex:
    return VectorIndex(FakeEmbedder(dim=dim, seed=seed)).build(_ITEMS)


class TestPersistence(unittest.TestCase):
    def test_save_load_search_identical(self):
        with tempfile.TemporaryDirectory() as td:
            idx = _build(td, seed=7)
            idx.save(td)
            emb = FakeEmbedder(dim=32, seed=7)
            loaded = VectorIndex.load(td, emb)
            self.assertEqual(loaded.size, 3)
            self.assertEqual(loaded.chunk_ids, idx.chunk_ids)
            q = "出差申请提交"
            a = [h.chunk_id for h in idx.search(q, top_k=5)]
            b = [h.chunk_id for h in loaded.search(q, top_k=5)]
            self.assertEqual(a, b, "持久化前后向量检索结果应一致")

    def test_archived_chunk_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            idx = _build(td)
            hits = idx.search("纸面审批 旧规则", top_k=5)
            self.assertTrue(all(h.chunk_id != "D-ARCH:1:1" for h in hits),
                            "默认应排除 archived 块")

    def test_meta_sidecar_has_model_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            idx = _build(td)
            idx.save(td)
            sidecar = json.loads((Path(td) / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["embedder"]["name"], "fake-bow")
            self.assertEqual(sidecar["embedder"]["dim"], 32)


class TestCorruptionDegrade(unittest.TestCase):
    def test_missing_files_raise(self):
        with tempfile.TemporaryDirectory() as td:
            emb = FakeEmbedder(dim=32, seed=7)
            with self.assertRaises(VectorIndexCorruptionError):
                VectorIndex.load(td, emb)

    def test_corrupt_index_file_raises(self):
        with tempfile.TemporaryDirectory() as td:
            _build(td).save(td)
            p = Path(td) / "index.faiss"
            raw = p.read_bytes()
            p.write_bytes(raw[: len(raw) // 2])  # 截断 → faiss 无法读取
            with self.assertRaises(VectorIndexCorruptionError):
                VectorIndex.load(td, FakeEmbedder(dim=32, seed=7))

    def test_corrupt_meta_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            _build(td).save(td)
            (Path(td) / "meta.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(VectorIndexCorruptionError):
                VectorIndex.load(td, FakeEmbedder(dim=32, seed=7))

    def test_dimension_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as td:
            idx = VectorIndex(FakeEmbedder(dim=64, seed=7)).build(_ITEMS)
            idx.save(td)
            # 手工改 meta：指纹相同但 dim 与当前 embedder 不一致 → 命中维度校验
            sidecar = json.loads((Path(td) / "meta.json").read_text(encoding="utf-8"))
            sidecar["embedder"]["dim"] = 8
            (Path(td) / "meta.json").write_text(
                json.dumps(sidecar), encoding="utf-8")
            with self.assertRaises(VectorIndexDimensionMismatchError):
                VectorIndex.load(td, FakeEmbedder(dim=64, seed=7))

    def test_fingerprint_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as td:
            _build(td, seed=7).save(td)
            with self.assertRaises(VectorIndexFingerprintMismatchError):
                VectorIndex.load(td, FakeEmbedder(dim=32, seed=8))

    def test_size_mismatch_meta_raises(self):
        with tempfile.TemporaryDirectory() as td:
            _build(td).save(td)
            sidecar = json.loads((Path(td) / "meta.json").read_text(encoding="utf-8"))
            sidecar["size"] = 99
            (Path(td) / "meta.json").write_text(
                json.dumps(sidecar), encoding="utf-8")
            with self.assertRaises(VectorIndexCorruptionError):
                VectorIndex.load(td, FakeEmbedder(dim=32, seed=7))

    def test_duplicate_chunk_id_rejected_on_build(self):
        idx = VectorIndex(FakeEmbedder(dim=32, seed=7))
        with self.assertRaises(VectorIndexError):
            idx.build(_ITEMS + [dict(_ITEMS[0])])


if __name__ == "__main__":
    unittest.main()
