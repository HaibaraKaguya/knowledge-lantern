"""配置加载 — YAML + 环境变量 → 全局 settings 单例.

优先级: 环境变量 > config.yaml > 硬编码默认值
配置项: api_key, api_base_url, model, temperature, timeout

用法: from src.config import settings; key = settings.api_key
"""

from __future__ import annotations
import os
from pathlib import Path
import yaml

_DEFAULTS = {"api_key": "", "api_base_url": "", "model": "gpt-4o-mini", "temperature": 0.3, "timeout": 120, "max_tokens": 16384}
# 🟡 环境变量名 "SRC_" 前缀不够明确，容易与其他项目冲突
_ENV_MAP = {"api_key": "SRC_API_KEY", "api_base_url": "SRC_API_BASE_URL", "model": "SRC_MODEL"}

class _Settings:
    def __init__(self):
        self._data = dict(_DEFAULTS)
        self._load_yaml(); self._apply_env()

    def _load_yaml(self):
        here = Path(__file__).resolve()  # → src/config.py
        # 搜索路径：项目根目录（含 pyproject.toml / .git）> src/ 目录
        # 不再遍历所有父目录直到 /，避免加载不相关的 config.yaml
        project_root = here.parent.parent  # src/ → 项目根
        search_dirs = [here.parent, project_root]
        for p in search_dirs:
            yp = p / "config.yaml"
            if yp.exists():
                with open(yp) as f:
                    d = yaml.safe_load(f) or {}
                if isinstance(d, dict): self._data.update(d)
                return

    def _apply_env(self):
        for k, v in _ENV_MAP.items():
            ev = os.environ.get(v)
            if ev is not None: self._data[k] = ev

    # 所有已知配置项的集合（__getattr__ 用于区分 typo 和合法缺失）
    _KNOWN_KEYS = set(_DEFAULTS.keys())

    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        if n in self._KNOWN_KEYS:
            return self._data.get(n, _DEFAULTS.get(n))
        raise AttributeError(f"未知配置项 '{n}'，可用: {sorted(self._KNOWN_KEYS)}")

# API Key 已从 config.yaml 中移除，使用环境变量 SRC_API_KEY 或配置文件填入
settings = _Settings()
