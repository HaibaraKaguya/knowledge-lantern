"""工具注册表 — 存储所有工具定义，支持按关键词搜索.

设计:
  - 200个工具存磁盘，只把搜出来的 3-5 个注入 prompt
  - tool_search(keyword) → LLM 需要时自己搜
  - tool_call(name, args) → 执行具体工具
"""

from __future__ import annotations
import json
import re
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolDef:
    """工具定义."""
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)  # JSON Schema
    endpoint: str = ""     # MCP server command, or "builtin"
    mcp_method: str = ""   # method name on the MCP server


# ═══════════════════════════════════════════════════════════════════
# 内置工具定义（5个）
# ═══════════════════════════════════════════════════════════════════

BUILTIN_TOOLS: list[ToolDef] = [
    ToolDef(
        name="web_search",
        description="搜索网页，返回标题、URL 和摘要。用于获取最新信息。",
        tags=["search", "web", "duckduckgo", "网页", "搜索"],
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        endpoint="builtin",
    ),
    ToolDef(
        name="read_page",
        description="读取指定 URL 的网页内容（提取纯文本）。",
        tags=["web", "fetch", "http", "网页", "读取"],
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "网页 URL"},
            },
            "required": ["url"],
        },
        endpoint="builtin",
    ),
    ToolDef(
        name="github_search_repos",
        description="搜索 GitHub 仓库，返回仓库名、描述、星数和 URL。",
        tags=["github", "code", "search", "repo", "仓库", "代码"],
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "language": {"type": "string", "default": ""},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
        endpoint="builtin",
    ),
    ToolDef(
        name="filesystem_read",
        description="读取本地文件内容。返回文件的文本内容。",
        tags=["file", "filesystem", "read", "文件", "读取"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件的绝对路径"},
                "max_chars": {"type": "integer", "default": 5000},
            },
            "required": ["path"],
        },
        endpoint="builtin",
    ),
    ToolDef(
        name="filesystem_list",
        description="列出目录下的文件和子目录。",
        tags=["file", "filesystem", "list", "directory", "目录", "文件"],
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录的绝对路径"},
            },
            "required": ["path"],
        },
        endpoint="builtin",
    ),
]


# ═══════════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════════

class ToolRegistry:
    """工具注册表：注册、搜索、注入 prompt."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        for t in BUILTIN_TOOLS:
            self.register(t)

    def register(self, tool: ToolDef):
        self._tools[tool.name] = tool

    def search(self, keyword: str, limit: int = 5) -> list[ToolDef]:
        """模糊搜索工具。按名称、描述、标签匹配，返回得分排序."""
        keyword = keyword.lower()
        scored = []
        for t in self._tools.values():
            score = 0
            if keyword in t.name.lower(): score += 10
            if keyword in t.description.lower(): score += 3
            for tag in t.tags:
                if keyword in tag.lower(): score += 8
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[:limit]]

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def all_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    def inject_prompt(self, tool_names: list[str]) -> str:
        """生成要注入 prompt 的工具描述（只包含指定的工具）."""
        if not tool_names:
            return ""
        lines = ["## 可用工具", ""]
        for name in tool_names:
            t = self._tools.get(name)
            if not t:
                continue
            params_desc = ", ".join(
                f"{k}: {v.get('type','str')}" + (f"={v.get('default','')}" if 'default' in v else "")
                for k, v in t.parameters.get("properties", {}).items()
            )
            lines.append(f"### {t.name}({params_desc})")
            lines.append(f"  {t.description}")
            lines.append("")
        lines.append(
            "使用工具时，输出 JSON: "
            '{"tool_call": "<工具名>", "arguments": {...}}'
        )
        lines.append(
            "搜索更多工具时，输出 JSON: "
            '{"tool_search": "<关键词>"}'
        )
        return "\n".join(lines)

    def tool_search_prompt(self) -> str:
        """注入到 prompt 的工具搜索提示."""
        return (
            "## 工具系统\n"
            "当前可能只有部分工具可见。如果需要其他能力，"
            '输出 {"tool_search": "<关键词>"} 来搜索更多工具。\n'
        )


# 全局单例
_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
