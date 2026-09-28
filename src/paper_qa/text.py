"""轻量词元化 — 与 src/knowledge/textutils.py 逻辑保持一致，自包含以便隔离。

paper_qa 包保持纯标准库依赖，不触发 src 包的重依赖导入。
FTS5 的 unicode61 分词器把连续中文当做一个 token，因此中文按
单字 + 相邻双字（unigram/bigram）摊平为空格分隔的词元再入检索。
"""

from __future__ import annotations
import re

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.fullmatch(ch))


def _dedupe(terms: list) -> list:
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _run_terms(run: list) -> list:
    out = []
    for i, ch in enumerate(run):
        out.append(ch)
        if i + 1 < len(run):
            out.append(ch + run[i + 1])
    return out


def cjk_terms(text: str) -> list:
    terms = []
    run = []
    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        else:
            terms.extend(_run_terms(run))
            run = []
    terms.extend(_run_terms(run))
    return _dedupe(terms)


def ascii_words(text: str) -> list:
    return [w.lower() for w in _WORD_RE.findall(text)]


def _run_weighted(run: list) -> list:
    out = []
    for i, ch in enumerate(run):
        out.append((ch, 1))
        if i + 1 < len(run):
            out.append((ch + run[i + 1], 2))
    return out


def query_terms_weighted(query: str) -> list:
    """查询 → 带权重词元 [(term, weight), ...]。ASCII 词=2，中文 bigram=2，单字=1。"""
    terms = []
    for w in ascii_words(query):
        terms.append((w, 2))
    run = []
    for ch in query:
        if _is_cjk(ch):
            run.append(ch)
        else:
            terms.extend(_run_weighted(run))
            run = []
    terms.extend(_run_weighted(run))
    return _dedupe(terms)


def fts_match_query(query: str) -> str | None:
    """查询 → FTS5 MATCH 表达式（词元 OR 连接，引号包裹防注入）。

    无词元（纯标点/空白）时返回 None，调用方按"无证据"处理。
    """
    terms = _dedupe(t for t, _ in query_terms_weighted(query))
    if not terms:
        return None
    return " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)


def overlap_score(text: str, terms: list) -> float:
    """文本与查询词元的加权重叠分。term 作为子串匹配（对中文与 ASCII 均有效）。"""
    score = 0.0
    for term, weight in terms:
        if term in text:
            score += weight
    return score
