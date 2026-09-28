"""自动路由 — 模型 JSON 路由器 + DIRECT 直接作答器.

路由职责：在“检索知识库证据作答”与“不依赖知识库直接作答”之间做模型自动选择。
- INTERNAL_RETRIEVAL：问题涉及公司/部门内部制度、流程、财务口径等，适合用
  知识库证据作答（默认路径；任何非法输出 / 调用失败一律回退到此）；
- DIRECT：开放性解释、观点、创作、泛建议、闲聊等不应硬套演示知识库的内容，
  只调用模型服务，绝不检索知识库（无 sources、无证据载荷）。

约束：
- 禁止用户手选路由 —— 请求模型不暴露 route 字段（见 schemas.PaperQAAskRequest）；
- 禁止“检索未命中 → 自动转 DIRECT” —— DIRECT 只能在检索前由路由器给出；
- 路由器调用只发生在服务端动作授权之后（见 service.PaperQAService.ask）。

实现：路由器要求模型服务仅输出严格 JSON，如 {"route": "DIRECT"}；
解析失败 / 值非法 / 调用异常都返回 INTERNAL_RETRIEVAL（fail-closed）。
"""

from __future__ import annotations
import json
import logging
from typing import Any

from .llm import AiunavilableError, PaperAnswerGenerator, extract_text

logger = logging.getLogger("knowledge_lantern.router")

ROUTE_INTERNAL_RETRIEVAL = "INTERNAL_RETRIEVAL"
ROUTE_DIRECT = "DIRECT"
_VALID_ROUTES = frozenset({ROUTE_INTERNAL_RETRIEVAL, ROUTE_DIRECT})


def build_classify_prompt(question: str) -> list[dict]:
    """构造路由器消息：要求模型仅输出严格 JSON 路由，禁止任何解释/代码块。"""
    system = (
        "你是 Knowledge Lantern 部门知识问答中心的自动路由分类器，只判断一个问题应该走哪条路径。\n"
        "可选路径：\n"
        '1. "INTERNAL_RETRIEVAL" —— 问题涉及公司/部门的制度、流程、政策、费用、报销、'
        "预算、财务、人事考勤等内部业务资料，需要（或可能）用内部知识库证据回答。\n"
        '2. "DIRECT" —— 问题不依赖上述内部资料：开放性解释、观点、创作、通用建议、'
        "闲聊，或明显不应以演示知识库为依据的内容。\n"
        "规则：只输出一个 JSON 对象，不要任何解释、不要代码块、不要多余字符。\n"
        '格式严格为 {"route": "INTERNAL_RETRIEVAL"} 或 {"route": "DIRECT"}。'
    )
    user = f"用户问题：{question}\n\n路由："
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_route(text: str) -> str | None:
    """从模型文本中宽容地提取 JSON 对象的 route 字段；任何异常返回 None。"""
    try:
        t = text.strip()
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            return None
        obj = json.loads(t[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    route = obj.get("route")
    return route if isinstance(route, str) else None


class RouteClassifier:
    """模型 JSON 路由器。

    client 可注入（测试用假客户端）；缺省懒加载 get_llm(temp=0.0)，
    构造与导入阶段不触网。classify() 永不抛错 —— 解析失败/值非法/调用异常
    一律返回 INTERNAL_RETRIEVAL（fail-closed，绝不误入 DIRECT）。
    """

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
        if self._model_name is not None and self._base_url is not None:
            return {"name": self._model_name, "base_url": self._base_url}
        from src.config import settings
        return {
            "name": self._model_name or settings.model,
            "base_url": self._base_url or settings.api_base_url,
        }

    def classify(self, question: str) -> str:
        """返回 ROUTE_INTERNAL_RETRIEVAL 或 ROUTE_DIRECT；任何失败回退检索路径。"""
        try:
            response = self._get_client().invoke(build_classify_prompt(question))
            route = _parse_route(extract_text(response))
        except Exception as e:  # 网络/超时/空响应/结构异常 → fail-closed
            logger.warning("route classifier call failed, fallback %s: %s",
                           ROUTE_INTERNAL_RETRIEVAL, e)
            return ROUTE_INTERNAL_RETRIEVAL
        if route not in _VALID_ROUTES:
            logger.warning("route classifier illegal output %r, fallback %s",
                           route, ROUTE_INTERNAL_RETRIEVAL)
            return ROUTE_INTERNAL_RETRIEVAL
        return route


def build_direct_prompt(question: str) -> list[dict]:
    """构造 DIRECT 作答消息：不携带任何知识库证据。"""
    system = (
        "你是 Knowledge Lantern 部门知识问答中心的直接作答器。当前问题不依赖内部知识库证据，"
        "请直接、清晰、有用地回答。\n"
        "规则:\n"
        "1. 使用与用户问题相同的语言回答。\n"
        "2. 不得编造 Knowledge Lantern/本部门的具体制度、流程、金额、报表等内部事实；"
        "若问题涉及这类内部资料，应说明需以知识库检索结果为准。\n"
        "3. 本次回答没有检索来源，不要声称自己检索过知识库，也不要引用证据编号。"
    )
    user = f"问题: {question}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


class DirectAnswerer(PaperAnswerGenerator):
    """DIRECT 路径作答器：仅调用模型服务，无检索证据。

    复用 PaperAnswerGenerator 的懒加载客户端 / model_info / ping；
    generate(question) 直接用普通对话提示作答（无证据块）。
    调用失败统一包装为 AiunavilableError（与检索路径语义一致）。
    """

    def __init__(self, client: Any | None = None,
                 model_name: str | None = None,
                 base_url: str | None = None):
        super().__init__(client=client, model_name=model_name, base_url=base_url)

    def generate(self, question: str) -> str:
        try:
            response = self._get_client().invoke(build_direct_prompt(question))
            return self._extract_text(response)
        except AiunavilableError:
            raise
        except Exception as e:
            raise AiunavilableError(f"模型服务调用失败: {e}") from e
