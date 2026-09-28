"""OpenAI-compatible 模型客户端工厂.

不依赖 langchain / langchain-openai；通过 httpx 直连任意提供
OpenAI-compatible API 的模型服务。关键点：
- httpx.Client(trust_env=False)：绝不读取系统 HTTP(S)_PROXY / NO_PROXY，
  本地 127.0.0.1 端点若走了系统代理会得到代理的 502/拒绝；
- 无云端回退：api_key 留空 + 本地端点 → 占位符；留空 + 远程端点 → 直接报错。

OpenAICompatibleClient.invoke(messages) 返回 ModelResponse（.content 属性为文本），
与上层生成器/查询改写器的既有接口一致，便于测试注入假客户端。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.config import resolve_api_key, settings


class ModelCallError(RuntimeError):
    """模型服务调用失败（HTTP 错误 / 超时 / 连接拒绝 / 响应结构异常）。"""


class ModelResponse:
    """最小完成响应：与 langchain AIMessage 的 .content 访问方式兼容。"""

    __slots__ = ("content", "raw")

    def __init__(self, content: str, raw: dict | None = None):
        self.content = content
        self.raw = raw or {}


class OpenAICompatibleClient:
    """OpenAI-compatible 客户端（模型名、端点和密钥由构造参数决定）。"""

    def __init__(self, *, base_url: str, model: str, api_key: str,
                 temperature: float, timeout: float | int, max_tokens: int,
                 http_client: httpx.Client | None = None):
        if not base_url:
            raise ValueError("base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        # 显式禁止继承系统代理，避免本地端点被意外转发。
        self._http = http_client or httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    def invoke(self, messages: list[dict]) -> ModelResponse:
        """调用 /chat/completions，返回 ModelResponse(content=文本)。"""
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        try:
            resp = self._http.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ModelCallError(f"模型请求超时（>{self.timeout}s）: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ModelCallError(f"模型服务连接失败: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelCallError(f"模型服务 HTTP {resp.status_code}: {resp.text[:300]}") from exc
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelCallError(
                f"模型服务响应结构异常: {exc}; body={resp.text[:300]}"
            ) from exc
        if not isinstance(text, str) or not text.strip():
            raise ModelCallError("模型服务返回空内容")
        return ModelResponse(content=text.strip(), raw=data)

    def model_info(self) -> dict:
        return {"name": self.model, "base_url": self.base_url}




def get_llm(temp: float | None = None, max_tokens: int | None = None) -> OpenAICompatibleClient:
    """创建 OpenAI-compatible 模型客户端（无 langchain 依赖）。"""
    key = resolve_api_key(settings.api_key, settings.api_base_url)
    return OpenAICompatibleClient(
        base_url=settings.api_base_url,
        model=settings.model,
        api_key=key,
        temperature=settings.temperature if temp is None else temp,
        timeout=settings.timeout,
        max_tokens=settings.max_tokens if max_tokens is None else max_tokens,
    )
