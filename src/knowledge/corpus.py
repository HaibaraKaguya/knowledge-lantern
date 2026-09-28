"""演示语料加载 — data/knowledge_demo/docs/<doc_id>/{metadata.json,content.md}."""

from __future__ import annotations
from pathlib import Path

from .metadata import load_document_from_files, MetadataError


def load_corpus(docs_dir) -> list:
    """加载 docs_dir 下所有文档目录，返回 [Document, ...]。"""
    base = Path(docs_dir)
    docs = []
    if not base.is_dir():
        return docs
    for entry in sorted(base.iterdir()):
        if entry.is_dir() and (entry / "metadata.json").exists():
            docs.append(load_document_from_files(entry))
    return docs


def load_qa(path) -> list:
    """加载问答评测数据 [{question, gold:{document_id, page?, contains?}}, ...]。"""
    p = Path(path)
    if not p.exists():
        return []
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data)
