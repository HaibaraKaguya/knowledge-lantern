"""FAISS 向量索引 + Embedder 抽象（离线确定性，可持久化）。

组件：
- Embedder 协议: name / dim / fingerprint / embed(texts)
- FakeEmbedder     —— 确定性词袋向量（离线、无 GPU、测试友好，名字明确含 fake）
- STEmbedder       —— 本地 BGE-M3 稠密向量（sentence-transformers，离线加载）
- FlagEmbeddingEmbedder —— BGE-M3（FlagEmbedding BGEM3FlagModel，离线；若该库/模型
  权重不兼容会抛 ImportError，由调用方按"向量组件不可用"降级）
- VectorIndex      —— FAISS cosine/IP 索引，chunk_id↔行号稳定映射，保存/加载 +
  指纹/维度/损坏校验 + 显式损坏错误

安全降级约定：VectorIndex.load() 对缺失/损坏/维度/指纹不一致抛出显式异常；
调用方（hybrid/paper_qa.retrieval）捕获后把向量组件标为 disabled 并保留 FTS 结果，
绝不静默报告 hybrid_rrf/vector 开启。

本模块在缺少 faiss/numpy 时仍可导入（仅 VectorIndex 受限），保证
knowledge 包"纯标准库离线"属性不被破坏。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

try:
    import numpy as np
    import faiss
except ImportError:  # pragma: no cover - 无 FAISS 环境仅影响 VectorIndex
    np = None
    faiss = None

from .textutils import tokenize

FORMAT_VERSION = 1

METRIC_COSINE = "cosine"
METRIC_IP = "ip"


class VectorIndexError(Exception):
    """向量索引通用错误。"""


class VectorIndexCorruptionError(VectorIndexError):
    """索引文件缺失/损坏/字段不一致。"""


class VectorIndexFingerprintMismatchError(VectorIndexError):
    """当前 embedder 指纹与索引构建时不一致（模型不符）。"""


class VectorIndexDimensionMismatchError(VectorIndexError):
    """当前 embedder 维度与索引维度不一致。"""


@runtime_checkable
class Embedder(Protocol):
    """嵌入器协议：可离线确定性生产向量，并自报指纹。"""

    name: str
    dim: int

    @property
    def fingerprint(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _model_fingerprint(model_path: str, dim: int, backend: str) -> str:
    h = hashlib.sha256(str(model_path).encode("utf-8")).hexdigest()[:12]
    return f"bge-m3:{dim}:{backend}:{h}"


class STEmbedder:
    """本地 BGE-M3 稠密向量 embedder（sentence-transformers 后端，离线）。

    model_path 优先解释为本地模型目录；不存在时交给
    sentence-transformers 按缓存模型名解析（HF_HUB_OFFLINE 生效时仅命中缓存）。
    加载失败抛 ImportError/OSError，由调用方降级为"向量组件不可用"。
    """

    def __init__(self, model_path: str, device: str = "cpu"):
        try:
            # 强制离线：本地模型缓存查找，绝不发起网络下载（避免无网环境长时间重试）
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "未安装 sentence-transformers，请先安装可选依赖: "
                ".venv/bin/python -m pip install 'sentence-transformers'"
            ) from e
        self.model_path = str(model_path)
        self._model = SentenceTransformer(self.model_path, device=device)
        get_dim = getattr(self._model, "get_embedding_dimension", None) or \
            getattr(self._model, "get_sentence_embedding_dimension")
        self._dim = int(get_dim())

    @property
    def name(self) -> str:
        return "bge-m3"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def fingerprint(self) -> str:
        return _model_fingerprint(self.model_path, self._dim, "sentence-transformers")

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        vecs = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False,
        )
        return np.asarray(vecs, dtype="float32").tolist()


class FlagEmbeddingEmbedder:
    """BGE-M3（FlagEmbedding BGEM3FlagModel，离线，无网络回退）。

    若 FlagEmbedding 未安装、模型权重不兼容或本地路径缺失 → 抛 ImportError/OSError。
    注意：BGEM3FlagModel 依赖的 transformers 版本与 bge-m3 快照可能不兼容；
    生产默认优先使用 STEmbedder，本类保留用于显式 backend=flagembedding。
    """

    def __init__(self, model_path: str, device: str = "cpu", use_fp16: bool = False):
        try:
            # 强制离线：只用本地模型目录/缓存（无网环境快速失败而非长时间重试）
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as e:
            raise ImportError(
                "未安装 FlagEmbedding，请先安装可选依赖: "
                ".venv/bin/python -m pip install 'FlagEmbedding>=1.3'"
            ) from e
        self.model_path = str(model_path)
        self._model = BGEM3FlagModel(
            self.model_path, devices=[device], use_fp16=use_fp16,
        )
        self._dim = int(self._model.model.config.hidden_size)

    @property
    def name(self) -> str:
        return "bge-m3"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def fingerprint(self) -> str:
        return _model_fingerprint(self.model_path, self._dim, "flagembedding")

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        out = self._model.encode(
            list(texts),
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return np.asarray(out["dense_vecs"], dtype="float32").tolist()


class FakeEmbedder:
    """确定性词袋向量 embedder（名字明确含 fake，仅供离线测试/验证）。

    token → 哈希索引累加权重。同一文本总是得到同一向量；共享词元的文本
    余弦相似度更高。绝不用作生产 embedding，产出也不代表任何模型语义。
    """

    def __init__(self, dim: int = 64, seed: int = 0):
        self.dim = int(dim)
        self._seed = int(seed)

    @property
    def name(self) -> str:
        return "fake-bow"

    @property
    def fingerprint(self) -> str:
        return f"fake-bow:{self.dim}:{self._seed}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokenize(text):
            vec[self._token_index(token)] += 1.0
        return vec

    def _token_index(self, token: str) -> int:
        h = hashlib.sha256(f"{self._seed}:{token}".encode("utf-8")).hexdigest()
        return int(h[:8], 16) % self.dim


def resolve_embedder(model: str, *, backend: str = "auto",
                     device: str = "cpu") -> Embedder:
    """按配置把模型路径解析为可用 Embedder。

    backend: auto | sentence_transformers | flagembedding | fake
      - fake：FakeEmbedder（仅测试/演示，明确非模型产出）
    backend=auto 时按 sentence-transformers → FlagEmbedding 顺序尝试。
    全部不可用 → 抛 ImportError/OSError（上层按"向量组件关闭"降级）。
    """
    if backend == "fake":
        return FakeEmbedder()
    if backend in ("sentence_transformers", "auto"):
        try:
            return STEmbedder(model, device=device)
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            if backend == "sentence_transformers":
                raise
            last_error = e
    else:
        last_error = ImportError(f"未知 backend: {backend}")
    if backend in ("flagembedding", "auto"):
        try:
            return FlagEmbeddingEmbedder(model, device=device)
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            last_error = e
    raise ImportError(
        f"无法加载 embedding 模型 {model!r}（backend={backend}）: {last_error}"
    ) from last_error


@dataclass
class VectorHit:
    """向量检索命中的块 + 原始元数据（索引构建时的描述快照）。"""

    chunk_id: str
    score: float
    row: int
    metadata: dict = field(default_factory=dict)


def _normalize(vecs) -> "np.ndarray":
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


class VectorIndex:
    """持久化 FAISS cosine/IP 索引，chunk_id↔行号稳定映射。

    build 按 items 传入顺序确定性建索引；save/load 使用
    <dir>/meta.json（chunk 映射 + 指纹/维度）+ <dir>/index.faiss。
    查询前校验当前 embedder 指纹与维度，防模型错配；损坏时抛显式错误。
    """

    def __init__(self, embedder: Embedder, metric: str = METRIC_COSINE):
        if metric not in (METRIC_COSINE, METRIC_IP):
            raise ValueError(f"未知 metric: {metric}（可选: {METRIC_COSINE}, {METRIC_IP}）")
        self.embedder = embedder
        self.metric = metric
        self.chunk_ids: list[str] = []
        self.metadata: dict[str, dict] = {}
        self._index = None
        self._vectors = None
        self._build_fingerprint: str | None = None
        self._build_dim: int | None = None

    @property
    def size(self) -> int:
        return len(self.chunk_ids)

    def __len__(self) -> int:
        return self.size

    def build(self, items: Iterable[dict]) -> "VectorIndex":
        """按传入顺序确定性建索引。items 至少含 chunk_id/text；建议含 meta。"""
        self._require_faiss()
        dim = int(self.embedder.dim)
        self.chunk_ids = []
        self.metadata = {}
        vecs = []
        texts: list[str] = []
        for item in items:
            cid = str(item["chunk_id"])
            if cid in self.metadata:
                raise VectorIndexError(f"重复 chunk_id: {cid}")
            text = item.get("text", "")
            if not isinstance(text, str) or not text:
                raise VectorIndexError(f"chunk {cid} 缺少文本")
            self.chunk_ids.append(cid)
            self.metadata[cid] = dict(item)
            texts.append(text)
        for cid, vec in zip(self.chunk_ids, self._embed(texts)):
            vecs.append(np.asarray(vec, dtype="float32"))
        self._vectors = np.stack(vecs) if vecs else np.zeros((0, dim), dtype="float32")
        self._build_fingerprint = self.embedder.fingerprint
        self._build_dim = dim
        self._rebuild_index()
        return self

    def _embed(self, texts: list[str]):
        if not texts:
            return []
        out = self.embedder.embed(texts)
        if len(out) != len(texts):
            raise VectorIndexError(
                f"embedder 返回条数 {len(out)} != 输入 {len(texts)}")
        return out

    def _rebuild_index(self):
        index = faiss.IndexFlatIP(self._build_dim)
        vecs = self._vectors
        if self.metric == METRIC_COSINE:
            vecs = _normalize(vecs)
        if vecs.shape[0]:
            index.add(np.ascontiguousarray(vecs, dtype="float32"))
        self._index = index

    def _validate_query_embedder(self):
        if self._build_fingerprint is None:
            raise VectorIndexError("索引未构建或加载")
        if self.embedder.fingerprint != self._build_fingerprint:
            raise VectorIndexFingerprintMismatchError(
                f"当前 embedder 指纹 {self.embedder.fingerprint} 与索引构建时 "
                f"{self._build_fingerprint} 不一致")
        if int(self.embedder.dim) != self._build_dim:
            raise VectorIndexDimensionMismatchError(
                f"当前 embedder 维度 {self.embedder.dim} 与索引维度 {self._build_dim} 不一致")

    def search(self, query: str, top_k: int = 10,
               include_archived: bool = False) -> list[VectorHit]:
        """向量 Top-K 近邻。默认排除 status='archived' 的块（索引快照）。"""
        if self._index is None or self.size == 0 or top_k <= 0:
            return []
        self._validate_query_embedder()
        qvec = np.asarray(self.embedder.embed([query])[0], dtype="float32").reshape(1, -1)
        if self.metric == METRIC_COSINE:
            qvec = _normalize(qvec)
        cand = min(self.size, max(top_k * 4, 64))
        scores, idxs = self._index.search(np.ascontiguousarray(qvec), cand)
        hits: list[VectorHit] = []
        for score, row in zip(scores[0], idxs[0]):
            if row < 0:
                continue
            cid = self.chunk_ids[row]
            meta = self.metadata.get(cid) or {}
            if not include_archived and meta.get("status") == "archived":
                continue
            hits.append(VectorHit(chunk_id=cid, score=float(score),
                                  row=int(row), metadata=meta))
            if len(hits) >= top_k:
                break
        return hits

    # ── 持久化 ─────────────────────────────────────────────

    def save(self, path: str | Path):
        """保存到目录: <path>/meta.json + <path>/index.faiss。"""
        self._require_faiss()
        if self._index is None:
            raise VectorIndexError("索引未构建，无法保存")
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        sidecar = {
            "format_version": FORMAT_VERSION,
            "embedder": {
                "name": self.embedder.name,
                "dim": self._build_dim,
                "fingerprint": self._build_fingerprint,
            },
            "metric": self.metric,
            "size": self.size,
            "chunk_ids": self.chunk_ids,
            "metadata": self.metadata,
        }
        (path / "meta.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        faiss.write_index(self._index, str(path / "index.faiss"))

    @classmethod
    def load(cls, path: str | Path, embedder: Embedder) -> "VectorIndex":
        """加载目录索引并做指纹/维度/完整性校验，损坏时抛显式错误。"""
        if faiss is None:
            raise RuntimeError("未安装 faiss，无法加载向量索引")
        path = Path(path)
        meta_path = path / "meta.json"
        index_path = path / "index.faiss"
        if not meta_path.exists() or not index_path.exists():
            raise VectorIndexCorruptionError(
                f"索引目录不完整: {path}（缺少 meta.json 或 index.faiss）")
        try:
            sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise VectorIndexCorruptionError(f"meta.json 不是合法 JSON: {e}") from e
        if not isinstance(sidecar, dict):
            raise VectorIndexCorruptionError("meta.json 顶层必须是对象")
        for key in ("format_version", "embedder", "metric", "size",
                    "chunk_ids", "metadata"):
            if key not in sidecar:
                raise VectorIndexCorruptionError(f"meta.json 缺少字段: {key}")
        emb = sidecar["embedder"]
        if not isinstance(emb, dict) or not all(k in emb for k in ("name", "dim", "fingerprint")):
            raise VectorIndexCorruptionError("meta.json embedder 字段损坏")
        if emb.get("fingerprint") != embedder.fingerprint:
            raise VectorIndexFingerprintMismatchError(
                f"索引属于模型指纹 {emb.get('fingerprint')}，当前为 "
                f"{embedder.fingerprint}，拒绝加载")
        try:
            sidecar_dim = int(emb.get("dim"))
        except (TypeError, ValueError):
            raise VectorIndexCorruptionError("meta.json embedder.dim 非法") from None
        if sidecar_dim != int(embedder.dim):
            raise VectorIndexDimensionMismatchError(
                f"索引维度 {sidecar_dim} 与当前 embedder 维度 {embedder.dim} 不一致")
        metric = sidecar.get("metric")
        if metric not in (METRIC_COSINE, METRIC_IP):
            raise VectorIndexCorruptionError(f"未知 metric: {metric}")
        try:
            size = int(sidecar["size"])
        except (TypeError, ValueError):
            raise VectorIndexCorruptionError("meta.json size 非法") from None
        chunk_ids = sidecar["chunk_ids"]
        metadata = sidecar["metadata"]
        if not isinstance(chunk_ids, list) or not isinstance(metadata, dict):
            raise VectorIndexCorruptionError("meta.json chunk_ids/metadata 类型非法")
        if len(chunk_ids) != size:
            raise VectorIndexCorruptionError(
                f"chunk_ids 数量 {len(chunk_ids)} 与 size {size} 不符")
        if len(metadata) != size:
            raise VectorIndexCorruptionError(
                f"metadata 数量 {len(metadata)} 与 size {size} 不符")
        if set(chunk_ids) != set(metadata):
            raise VectorIndexCorruptionError("chunk_ids 与 metadata 的键不一致")
        if len(set(chunk_ids)) != len(chunk_ids):
            raise VectorIndexCorruptionError("chunk_ids 存在重复")
        try:
            index = faiss.read_index(str(index_path))
        except Exception as e:
            raise VectorIndexCorruptionError(f"index.faiss 无法读取: {e}") from e
        if index.d != sidecar_dim:
            raise VectorIndexDimensionMismatchError(
                f"FAISS 索引维度 {index.d} 与 meta.json 维度 {sidecar_dim} 不一致")
        if index.ntotal != size:
            raise VectorIndexCorruptionError(
                f"FAISS 索引条数 {index.ntotal} 与 meta.json size {size} 不符")
        obj = cls(embedder, metric=metric)
        obj.chunk_ids = chunk_ids
        obj.metadata = metadata
        obj._index = index
        obj._vectors = None
        obj._build_fingerprint = str(emb["fingerprint"])
        obj._build_dim = sidecar_dim
        return obj

    @staticmethod
    def _require_faiss():
        if faiss is None:
            raise RuntimeError("未安装 faiss，请先安装: "
                               ".venv/bin/python -m pip install faiss-cpu")


def build_from_store_rows(rows: Iterable[dict], embedder: Embedder,
                          metric: str = METRIC_COSINE) -> VectorIndex:
    """从检索行（含 chunk_id/text/document_id/.../meta）构建 VectorIndex。

    rows 顺序即索引行序；每行 meta 会作为该 chunk 的构建时描述快照
    （含 status，供查询时归档过滤）。
    """
    items = []
    for r in rows:
        items.append({
            "chunk_id": r["chunk_id"],
            "text": r.get("text", ""),
            "heading_path": r.get("heading_path", ""),
            "page": int(r.get("page", 1)),
            "document_id": r.get("document_id", ""),
            "title": r.get("title", ""),
            "version": r.get("version", ""),
            "status": r.get("status", "active"),
            "meta": r.get("meta") or {},
        })
    return VectorIndex(embedder, metric=metric).build(items)
