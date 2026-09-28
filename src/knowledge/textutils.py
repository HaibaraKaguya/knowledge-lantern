"""词元化工具 — 让 SQLite FTS5 也能对中文做词级检索.

SQLite FTS5 的 unicode61 分词器会把连续中文当做一个 token，
导致"事故上报"匹配不上"生产事故上报"这类子串。这里把中文按
单字 + 相邻双字（unigram/bigram）摊平为空格分隔的词元再入索引：
  原文: "生产事故上报"
  索引: 生产 事故 上报 产事 故上
查询时把用户问题转成同样的词元集合做 AND 匹配，得到词级检索基线。
"""

from __future__ import annotations
import re

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.fullmatch(ch))


def cjk_terms(text: str) -> list:
    """提取文本中的中文单字/双字词元（去掉相邻重复）。"""
    terms = []
    run = []
    for ch in text:
        if _is_cjk(ch):
            run.append(ch)
        else:
            terms.extend(_run_terms(run)); run = []
    terms.extend(_run_terms(run))
    return _dedupe(terms)


def _run_terms(run: list) -> list:
    out = []
    for i, ch in enumerate(run):
        out.append(ch)
        if i + 1 < len(run):
            out.append(ch + run[i + 1])
    return out


def _dedupe(terms: list) -> list:
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def ascii_words(text: str) -> list:
    """提取英文/数字词（小写）。"""
    return [w.lower() for w in _WORD_RE.findall(text)]


def tokenize(text: str) -> list:
    """文本 → 索引词元集合（ASCII 词 + 中文单/双字）。"""
    terms = ascii_words(text) + cjk_terms(text)
    return _dedupe(terms)


def searchable(text: str) -> str:
    """文本 → 进入 FTS 索引的"可检索字符串"（原文 + 词元流）。"""
    return f"{text}\n{' '.join(tokenize(text))}"


def query_terms_weighted(query: str) -> list:
    """查询 → 带权重的词元列表 [(term, weight), ...]。

    权重: ASCII 词=2，中文双字 bigram=2，中文单字 unigram=1。
    用于对候选块做词元重叠打分（召回友好，能处理整句自然语言问题）。
    """
    terms = []
    for w in ascii_words(query):
        terms.append((w, 2))
    # 按中文连续段生成 unigram + bigram，并标注权重
    run = []
    for ch in query:
        if _is_cjk(ch):
            run.append(ch)
        else:
            terms.extend(_run_weighted(run)); run = []
    terms.extend(_run_weighted(run))
    seen, out = set(), []
    for t, w in terms:
        if t not in seen:
            seen.add(t); out.append((t, w))
    return out


def _run_weighted(run: list) -> list:
    out = []
    for i, ch in enumerate(run):
        out.append((ch, 1))                      # unigram
        if i + 1 < len(run):
            out.append((ch + run[i + 1], 2))     # bigram
    return out


def overlap_score(text: str, terms: list) -> float:
    """文本与查询词元的加权重叠分。term 作为子串匹配（对中文与 ASCII 均有效）。"""
    score = 0.0
    for term, weight in terms:
        if term in text:
            score += weight
    return score


def fts_match_query(query: str) -> str | None:
    """把查询转成 FTS5 MATCH 表达式（词元 OR 连接 → 高召回候选）。

    bigram 跨数字/标点会断（如"4 小时内"的"故上"），因此用 OR 而非 AND，
    靠 bm25 对命中更多词元的块排序；最终相关度由 retrieve() 的词元重叠分决定。
    """
    terms = _dedupe(t for t, _ in query_terms_weighted(query))
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)
