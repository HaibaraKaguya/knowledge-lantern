"""LLM 封装 — ChatOpenAI 工厂 + LLM 返回的 JSON 安全解析.

get_llm(temp)    — 创建 ChatOpenAI 实例（从 settings 读取配置）
safe_parse_json  — 4 层回退解析 LLM 返回的 JSON（markdown提取→直接解析→去尾逗号→正则兜底）
"""

from __future__ import annotations
import json, re
from langchain_openai import ChatOpenAI
from src.config import settings


def safe_parse_json(text: str, fallback: dict | None = None) -> dict:
    """安全解析 LLM 返回的 JSON，4 层回退。

    如果 text 不是字符串（LLM 可能返回 list/dict/None），直接返回 fallback。
    """
    if fallback is None: fallback = {}

    # 类型检查：LLM 可能返回非字符串类型
    if not isinstance(text, str):
        return fallback

    s = text.strip()
    if not s:
        return fallback

    # 尝试1: 提取 markdown code block 中的 JSON
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if m: s = m.group(1).strip()

    # 尝试2: 直接解析
    try: return json.loads(s)
    except json.JSONDecodeError: pass

    # 尝试3: 移除尾部逗号（仅当 JSON 字符串内不含 ,} 或 ,] 时才操作）
    # 检测字符串内的 ",}" / ",]" —— 如果存在则跳过此修复（避免破坏字符串内容）
    if not re.search(r'"\s*,\s*[}\]]', s):
        try: return json.loads(re.sub(r",\s*([}\]])", r"\1", s))
        except json.JSONDecodeError: pass

    # 尝试4: 提取第一个 {...} 对象
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except json.JSONDecodeError: pass

    return fallback


def get_llm(temp: float | None = None, max_tokens: int | None = None) -> ChatOpenAI:
    """创建 ChatOpenAI 实例。max_tokens 不传时根据温度自动选值."""
    key = settings.api_key
    if not key or len(key) < 10:
        raise RuntimeError("未配置 API Key。cp config.yaml.example config.yaml")
    kw = {"model": settings.model, "temperature": temp or settings.temperature,
          "api_key": key, "timeout": settings.timeout}
    if settings.api_base_url: kw["base_url"] = settings.api_base_url
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    elif "gpt" not in settings.model:
        kw["max_tokens"] = 16384  # StepFun/DeepSeek 等兼容模型，默认 16K 输出
    return ChatOpenAI(**kw)
