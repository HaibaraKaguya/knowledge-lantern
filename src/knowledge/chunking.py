"""结构优先的文档切分（structure-first chunking).

切分边界遵循文档结构本身，而非固定字符窗口：
  1. 页面标记（--- page N --- 或 <<page N>>）→ 页面
  2. 页面内 Markdown 标题（# / ## / ###）→ 小节
  3. 小节按空行分隔的段落再次拆分（仅当 max_chars 限制时）

每个 Chunk 保留出处：document_id / page / version / status / heading_path。
"""

from __future__ import annotations
import re
from .models import Chunk, Document
from .metadata import inherit_metadata

_PAGE_RE = re.compile(r"(?:---\s*page\s+(\d+)\s*---|<<page\s+(\d+)\s*>>)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
# 以标题开头的整块（含多级标题），一次切出一个（header, 后续文本）
_BLOCK_RE = re.compile(r"^(#{1,4})\s+.*$", re.MULTILINE)


def _split_pages(content: str):
    """把正文按页面标记切分成 [(page_no, text), ...]。无标记则整篇为 page 1。

    约定: "--- page N ---" 标记之后的正文归属第 N 页；
    首个标记之前的引言归属其前一页（N-1，若 N>1）。
    """
    marks = list(_PAGE_RE.finditer(content))
    if not marks:
        return [(1, content)]
    pages = []
    first_page = int(marks[0].group(1) or marks[0].group(2))
    preamble = content[:marks[0].start()].strip("\n")
    if preamble:
        pages.append((first_page - 1 if first_page > 1 else 1, preamble))
    for i, m in enumerate(marks):
        page_no = int(m.group(1) or m.group(2))
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        text = content[start:end].strip("\n")
        pages.append((page_no, text))
    return pages


def _section_blocks(text: str):
    """把页面文本按标题切成 [(heading_path, [lines]), ...]，保留标题层级。

    返回的每块 = 一个标题及其下属正文（直到下一个同级或更高标题为止）。
    """
    lines = text.splitlines()
    blocks = []                 # (level, heading, [body lines])
    current = None
    for ln in lines:
        m = _HEADING_RE.match(ln)
        if m:
            if current is not None:
                blocks.append(current)
            current = [len(m.group(1)), m.group(2).strip(), []]
        else:
            if current is None:
                # 首个标题之前的引言，归入一个隐式块
                current = [0, "", []]
            current[2].append(ln)
    if current is not None:
        blocks.append(current)
    return blocks


_SENT_RE = re.compile(r"(?<=[。！？；])\s*")


def _split_body(body_lines: list, max_chars: int | None):
    """按段落拆分正文；超长段落再按句子边界切分（结构优先兜底）。"""
    text = "\n".join(body_lines).strip()
    if not text:
        return []
    if max_chars is None or len(text) <= max_chars:
        return [text]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for para in paragraphs:
        out.extend(_split_long(para, max_chars))
    return out


def _split_long(text: str, max_chars: int) -> list:
    """把长段落按句子切分后贪心打包成 ≤ max_chars 的块；单句超长则硬切。"""
    sentences = [s.strip() for s in _SENT_RE.split(text) if s.strip()]
    chunks, buf = [], ""
    for s in sentences:
        if len(s) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(s), max_chars):
                chunks.append(s[i:i + max_chars])
            continue
        if len(buf) + len(s) + 1 <= max_chars:
            buf = f"{buf}{s}" if buf else s
        else:
            chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks


def chunk_document(doc: Document, max_chars: int | None = None) -> list:
    """结构优先切分。返回 [Chunk, ...]，按页面与节序排列。

    每个 chunk 携带文档级继承元数据快照（inherit_metadata），供持久化与展示；
    授权判定仍以 documents 表当前列为准，不依赖本快照。
    """
    inherited = inherit_metadata(doc)
    chunks = []
    seq = 0
    for page_no, page_text in _split_pages(doc.content):
        for level, heading, body in _section_blocks(page_text):
            heading_path = ("#" * level + " " + heading) if heading else "引言"
            for piece in _split_body(body, max_chars):
                seq += 1
                chunks.append(Chunk(
                    chunk_id=f"{doc.document_id}:{page_no}:{seq}",
                    document_id=doc.document_id,
                    page=page_no,
                    version=doc.version,
                    status=doc.status,
                    heading_path=heading_path,
                    seq=seq,
                    text=piece,
                    metadata=dict(inherited),
                ))
    return chunks
