"""配置加载 — YAML + 环境变量 → 全局 settings 单例.

优先级: 环境变量 > config.yaml > 内置默认值
配置项: api_key, api_base_url, model, temperature, timeout, max_tokens

默认端点是本机的 OpenAI-compatible 服务：
  http://127.0.0.1:8080/v1  +  可配置模型名
兼容任何提供 /v1/chat/completions 接口的本地或远程服务。

用法: from src.config import settings; key = settings.api_key
"""

from __future__ import annotations
import os
from pathlib import Path
import yaml

# 默认端点 = 本机 OpenAI-compatible /v1 端点。
# 注意: 若项目根存在 config.yaml，其值会覆盖此处默认值。
_DEFAULTS = {
    "api_key": "",                              # 本地服务通常可留空
    "api_base_url": "http://127.0.0.1:8080/v1", # OpenAI-compatible /v1 端点
    "model": "local-model",
    "temperature": 0.3,
    "timeout": 120,
    "max_tokens": 8192,
}
_ENV_MAP = {
    "api_key": "SRC_API_KEY",
    "api_base_url": "SRC_API_BASE_URL",
    "model": "SRC_MODEL",
    "temperature": "SRC_TEMPERATURE",
    "timeout": "SRC_TIMEOUT",
    "max_tokens": "SRC_MAX_TOKENS",
}


def is_local_endpoint(url: str) -> bool:
    """判断 base_url 是否指向本地模型服务（loopback / unix socket）。"""
    if not url:
        return False
    u = url.strip().lower()
    return ("127.0.0.1" in u or "localhost" in u
            or u.startswith("unix:") or u.startswith("http://0.0.0.0"))


def resolve_api_key(key: str, base_url: str) -> str:
    """解析出传给 OpenAI 兼容客户端的 api_key。

    - 已配置 key → 原样返回
    - 空 key + 本地端点 → 占位符 "not-needed"
    - 空 key + 远程端点 → 报错（不做云端回退）
    """
    if key:
        return key
    if is_local_endpoint(base_url):
        return "not-needed"
    raise RuntimeError(
        "未配置 API Key，且端点不是本地模型服务。"
        "本地端点可留空 api_key；远程端点请在 config.yaml 或 SRC_API_KEY 中设置。"
    )

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
            if ev is None: continue
            # 按默认值类型做强制转换（temperature→float，timeout/max_tokens→int）
            default = _DEFAULTS.get(k)
            if isinstance(default, bool): self._data[k] = ev.lower() in ("1", "true", "yes")
            elif isinstance(default, int): self._data[k] = int(ev)
            elif isinstance(default, float): self._data[k] = float(ev)
            else: self._data[k] = ev

    # 所有已知配置项的集合（__getattr__ 用于区分 typo 和合法缺失）
    _KNOWN_KEYS = set(_DEFAULTS.keys())

    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        if n in self._KNOWN_KEYS:
            return self._data.get(n, _DEFAULTS.get(n))
        raise AttributeError(f"未知配置项 '{n}'，可用: {sorted(self._KNOWN_KEYS)}")

# API Key 已从 config.yaml 中移除，使用环境变量 SRC_API_KEY 或配置文件填入
settings = _Settings()
