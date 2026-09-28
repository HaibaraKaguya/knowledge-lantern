"""可选 BGE reranker 封装 — 融合后精排（无模型时安全降级为不启用）.

组件契约：
  - Reranker.reason 输出名（如 "bge-reranker-v2-m3"）
  - rerank_scores(query, texts) -> list[float]（分数越大越相关，单调可用）
  - 模型缺失/库缺失 → 初始化抛 ImportError/OSError，上层把 reranker 标为
    disabled 并把该事实写进状态，绝不静默声称 rerank 已开启。

FakeReranker 仅用于离线测试（名字明确含 fake）：可注入固定分数映射，
用于验证"rerank 启用会改变排序"且状态如实反映。
"""

from __future__ import annotations

import os
from pathlib import Path

RERANKER_BACKEND = "bge-reranker-v2-m3"

#: 判定"目录是一个可加载的本地模型"所需文件（不下载、不改模型）。
_WEIGHT_FILES = (
    "model.safetensors", "pytorch_model.bin",
    "model.safetensors.index.json", "pytorch_model.bin.index.json",
)
_TOKENIZER_FILES = (
    "tokenizer.json", "sentencepiece.bpe.model", "spiece.model", "tokenizer_config.json",
)


def _hub_cache_dir() -> Path:
    hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub:
        return Path(hub)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _looks_like_model_dir(p: Path) -> bool:
    if not p.is_dir() or not (p / "config.json").exists():
        return False
    has_weights = any((p / w).exists() for w in _WEIGHT_FILES)
    has_tokenizer = any((p / t).exists() for t in _TOKENIZER_FILES)
    return has_weights and has_tokenizer


def resolve_local_model_dir(model_path: str) -> str | None:
    """把模型名/路径解析为**本地**模型目录（绝不下载）。

    解析顺序：
      1. model_path 本身是本地目录 → 原样返回；
      2. 否则在本地 HF 缓存 `models--<org>--<name>/snapshots/*` 中找可用快照；
      3. 都没有 → None（离线环境下上层据此安全降级，不联网重试）。
    """
    if not model_path:
        return None
    p = Path(model_path).expanduser()
    if _looks_like_model_dir(p):
        return str(p)
    if p.is_dir():  # 目录存在但缺权重/tokenizer：仍返回，交给加载器报明确错误
        return str(p)
    repo = "models--" + model_path.replace("/", "--")
    snaps = _hub_cache_dir() / repo / "snapshots"
    if snaps.is_dir():
        for snap in sorted(snaps.iterdir()):
            if _looks_like_model_dir(snap):
                return str(snap)
    return None


def dependency_status() -> dict:
    """reranker 依赖（FlagEmbedding）可用性探测（不导入重库）。"""
    import importlib.util
    spec = importlib.util.find_spec("FlagEmbedding")
    return {
        "name": "FlagEmbedding",
        "installed": spec is not None,
        "hint": "未安装 FlagEmbedding（reranker 依赖），安装: "
                ".venv/bin/python -m pip install 'FlagEmbedding>=1.3'"
                if spec is None else None,
    }



class RerankerUnavailableError(ImportError):
    """reranker 模型或可选依赖不可用（上层捕获后标 disabled）。"""


class FlagReranker:
    """BGE reranker（FlagReranker，离线本地模型路径）。"""

    def __init__(self, model_path: str, device: str = "cpu", use_fp16: bool = False):
        self.model_path = str(model_path)
        if not os.path.isdir(self.model_path):
            raise RerankerUnavailableError(
                f"reranker 模型目录不存在: {self.model_path}")
        try:
            from FlagEmbedding import FlagReranker as _FlagReranker
        except ImportError as e:
            raise RerankerUnavailableError(
                "未安装 FlagEmbedding（reranker 依赖），请先安装可选依赖: "
                ".venv/bin/python -m pip install 'FlagEmbedding>=1.3'"
            ) from e
        try:
            self._model = _FlagReranker(self.model_path, device=device, use_fp16=use_fp16)
        except Exception as e:
            raise RerankerUnavailableError(
                f"reranker 模型加载失败: {self.model_path}: {e}") from e

    @property
    def name(self) -> str:
        return RERANKER_BACKEND

    def rerank_scores(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        try:
            out = self._model.compute_score(
                [[query, t] for t in texts], normalize=True)
        except Exception as e:
            raise RerankerUnavailableError(f"reranker 打分失败: {e}") from e
        if isinstance(out, (int, float)):
            return [float(out)]
        return [float(s) for s in out]


class FakeReranker:
    """测试用确定性 reranker：按注入分数重排（不加载任何模型）。"""

    def __init__(self, scores: list[float] | None = None, *, name: str = "fake-reranker"):
        self.scores = list(scores) if scores is not None else None
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def rerank_scores(self, query: str, texts: list[str]) -> list[float]:
        if self.scores is not None:
            return [float(self.scores[i]) for i in range(len(texts))]
        # 默认：升序打分 → 排序后等价于把输入顺序反转（用于验证"重排确实生效"）
        return [float(i + 1) for i in range(len(texts))]


def resolve_reranker(model_path: str, *, device: str = "cpu",
                     backend: str = "auto"):
    """按配置解析 reranker 实例；不可用抛 RerankerUnavailableError。

    模型解析只使用本地目录 / 本地 HF 缓存（resolve_local_model_dir），
    离线环境绝不发起下载；不可用即抛错，由上层安全降级并如实报告。
    """
    if backend == "fake":
        return FakeReranker()
    resolved = resolve_local_model_dir(model_path)
    if resolved is None:
        raise RerankerUnavailableError(
            f"reranker 不可用：{model_path!r} 既不是本地模型目录，"
            "本地 HF 缓存中也没有可用快照；离线环境无法下载（不自动联网）")
    return FlagReranker(resolved, device=device)
