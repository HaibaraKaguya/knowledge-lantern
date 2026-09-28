"""模型生成器 — 依据检索证据生成部门知识回答.

client 可注入（测试用假客户端），默认懒加载 get_llm(temp=0.0)。
生成严格基于检索证据、引用来源编号、证据不足时明说不足而非猜测。
"""

from __future__ import annotations
from typing import Any

from .retrieval import RetrievedChunk

_ROUTE = "INTERNAL_RETRIEVAL"


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> list[dict]:
    """构造系统/用户消息。chunks 编号为 [1]..[N]。"""
    blocks = []
    for i, ch in enumerate(chunks, start=1):
        blocks.append(
            f"[{i}] {ch.title}\n"
            f"    文档ID: {ch.document_id} / 知识条目: {ch.heading_path}\n"
            f"    正文: {ch.text}"
        )
    evidence_text = "\n\n".join(blocks) or "（未检索到相关证据）"

    system = (
        "你是部门知识问答中心的作答器。请严格只依据下方提供的知识条目证据回答用户问题。\n"
        "规则:\n"
        "1. 每个陈述都必须引用证据编号，例如 [1]、[2]。\n"
        "2. 如果提供的证据不足以回答该问题，必须明确回答“现有资料不足以回答该问题”，"
        "不得猜测或编造。\n"
        "3. 不要使用任何外部知识，不要补充证据之外的事实；不要给出与证据无关的政策建议。\n"
        "4. 使用与用户问题相同的语言回答。\n"
        "5. 证据中的数值、参数与步骤均为演示用业务配置；回答时按证据原文转述，"
        "不要声称其代表任何真实公司的财务或经营数据。"
    )
    user = f"问题: {question}\n\n证据:\n{evidence_text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class AiunavilableError(Exception):
    """模型服务调用失败（网络/服务不可达/超时/异常响应/空内容）。"""


def extract_text(response: Any) -> str:
    """从响应提取文本；空内容视为失败而非成功空白答案。

    兼容两种响应形态：
    - 带 .content 属性的对象（ModelResponse / langchain 风格）；
    - 纯字符串。
    content 为 None / 空串 / 全空白 → 抛 AiunavilableError。
    """
    content = getattr(response, "content", response)
    if content is None:
        raise AiunavilableError("模型服务返回空内容")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("text")
                if t:
                    parts.append(t)
            else:
                parts.append(str(block))
        text = "\n".join(parts)
    else:
        text = str(content)
    text = text.strip()
    if not text:
        raise AiunavilableError("模型服务返回空内容")
    return text


class PaperAnswerGenerator:
    """把检索结果交给模型服务生成答案。client 缺省为懒加载的通用客户端。"""

    def __init__(self, client: Any | None = None,
                 model_name: str | None = None,
                 base_url: str | None = None):
        self._client = client
        self._model_name = model_name
        self._base_url = base_url

    def _get_client(self):
        if self._client is not None:
            return self._client
        from .client import get_llm
        return get_llm(temp=0.0)

    def model_info(self) -> dict:
        """返回模型名与端点。缺省时懒加载 src.config.settings。"""
        if self._model_name is not None and self._base_url is not None:
            return {"name": self._model_name, "base_url": self._base_url}
        from src.config import settings
        return {
            "name": self._model_name or settings.model,
            "base_url": self._base_url or settings.api_base_url,
        }

    def generate(self, question: str, chunks: list[RetrievedChunk]) -> str:
        messages = build_prompt(question, chunks)
        try:
            response = self._get_client().invoke(messages)
            return self._extract_text(response)
        except AiunavilableError:
            raise
        except Exception as e:  # 网络/超时/协议错误 → 统一包装为不可用
            raise AiunavilableError(f"模型服务调用失败: {e}") from e

    def ping(self) -> dict:
        """健康探测：极小的 invoke，空响应视为不可用，成功/失败都返回结构化状态。"""
        info = self.model_info()
        try:
            response = self._get_client().invoke([{"role": "user", "content": "ping"}])
            extract_text(response)  # 空内容 → AiunavilableError
            return {"status": "available", "error": None, **info}
        except Exception as e:
            return {"status": "unavailable", "error": str(e), **info}

    @staticmethod
    def _extract_text(response: Any) -> str:
        return extract_text(response)
