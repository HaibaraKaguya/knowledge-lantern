"""查询改写（轻量） — 中文问题 → 用于 FTS 检索的检索词.

演示语料为结构化中文业务知识，FTS5 使用中文单字/双字词元检索，
改写只需要做"轻清洗"：去掉开头寒暄/意图词，保留问题主体作为检索词。
改写结果会随回答一起返回（retrieval_query 字段），用于定位“用哪个检索词
命中了哪些证据”——因此改写必须是确定性的、可读的，且不依赖模型。

模型调用失败、注入内容等一律不涉及：本模块不调用任何 LLM。
"""

from __future__ import annotations
import re

#: 行首常见寒暄/意图词 + 随后的标点/空白，去掉后可提升检索词纯度。
#: 支持连续出现（“麻烦帮我查一下…”），重试剥离直到稳定。
_LEADING_FILLER_RE = re.compile(
    r"^(?:您好|你好|嗨|哈喽|请问|想问|麻烦|帮我|我想要|我想知道|请告诉|"
    r"告诉我|解释一下|说下|介绍下|说明下|查一下|看看|帮我看下|有没有)?"
    r"[\s,，.。、:：!！?？;；]*"
)
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_LEN = 80


def _strip_leading_fillers(q: str) -> str:
    for _ in range(5):
        stripped = _LEADING_FILLER_RE.sub("", q)
        if stripped == q:
            break
        q = stripped
    return q.strip()


class QueryRewriter:
    """确定性查询清洗：不调用模型，失败不可能（纯字符串处理）。"""

    def __init__(self, client=None, model_name=None, base_url=None):
        # 保留可选参数以兼容旧注入方式；本实现不依赖任何模型客户端。
        pass

    def rewrite(self, question: str | None) -> str:
        """返回用于 FTS 检索的词串；任何异常都安全回退为原问题。"""
        q = (question or "").strip()
        if not q:
            return q
        try:
            q = _strip_leading_fillers(q)
            q = _WHITESPACE_RE.sub(" ", q)
            q = re.sub(r"[？?。.！!]+$", "", q)
            q = re.sub(r"[\r\n]+", " ", q)
            if len(q) > _MAX_LEN:
                q = q[:_MAX_LEN]
            return q
        except Exception:
            return (question or "").strip()
