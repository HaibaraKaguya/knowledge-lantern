"""工具执行器 — 真正执行工具调用."""

from __future__ import annotations
import json
import os
import re
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path


def execute_tool(name: str, arguments: dict) -> str:
    """执行工具，返回文本结果."""
    if name == "web_search":
        return _web_search(arguments.get("query", ""), arguments.get("max_results", 5))
    elif name == "read_page":
        return _read_page(arguments.get("url", ""))
    elif name == "github_search_repos":
        return _github_search(arguments.get("query", ""),
                              arguments.get("language", ""),
                              arguments.get("max_results", 5))
    elif name == "filesystem_read":
        return _filesystem_read(arguments.get("path", ""),
                                arguments.get("max_chars", 5000))
    elif name == "filesystem_list":
        return _filesystem_list(arguments.get("path", ""))
    else:
        return f"未知工具: {name}"


def _web_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo 搜索（免费，无 API Key）."""
    if not query:
        return "错误: query 不能为空"

    # DuckDuckGo HTML 搜索（非官方 API，但免费无需 key）
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Shuchiin-Academy/2.1"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"搜索失败: {e}"

    # 提取结果
    results = []
    # 每个结果的标题和片段
    link_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',
        re.IGNORECASE
    )
    snippet_pattern = re.compile(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (href, title) in enumerate(links[:max_results]):
        if title.strip():
            snippet = _clean_html(snippets[i]) if i < len(snippets) else ""
            results.append(f"{i+1}. **{title.strip()}**\n   URL: {href}\n   {snippet[:200]}")

    if not results:
        return f"未找到与 '{query}' 相关的结果"

    return f"搜索 '{query}' 结果:\n\n" + "\n\n".join(results)


def _read_page(url: str) -> str:
    """读取网页内容，提取纯文本."""
    if not url:
        return "错误: url 不能为空"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Shuchiin-Academy/2.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"读取失败: {e}"

    # 简单提取文本：去掉 script/style 标签，去掉 HTML 标签
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) > 5000:
        text = text[:5000] + "\n... (内容截断)"

    return f"页面内容 ({url}):\n\n{text}"


def _github_search(query: str, language: str = "", max_results: int = 5) -> str:
    """GitHub API 搜索仓库（免费，不需要 token 但有限速）."""
    if not query:
        return "错误: query 不能为空"

    q = query
    if language:
        q += f"+language:{language}"
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&per_page={max_results}&sort=stars"

    req = urllib.request.Request(url, headers={
        "User-Agent": "Shuchiin-Academy/2.1",
        "Accept": "application/vnd.github.v3+json",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return f"GitHub API 错误: HTTP {e.code} (可能是限速，稍后再试)"
    except Exception as e:
        return f"GitHub 搜索失败: {e}"

    items = data.get("items", [])
    if not items:
        return f"未找到匹配 '{query}' 的 GitHub 仓库"

    results = []
    for i, item in enumerate(items[:max_results]):
        results.append(
            f"{i+1}. **{item['full_name']}** ⭐{item['stargazers_count']}\n"
            f"   {item.get('description', '无描述')[:200]}\n"
            f"   URL: {item['html_url']}"
        )

    return f"GitHub 搜索 '{query}':\n\n" + "\n\n".join(results)


def _filesystem_read(path: str, max_chars: int = 5000) -> str:
    """读取本地文件."""
    if not path:
        return "错误: path 不能为空"

    p = Path(path)
    if not p.exists():
        return f"文件不存在: {path}"
    if p.is_dir():
        return f"'{path}' 是目录，不是文件"

    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"无法读取二进制文件: {path}"
    except Exception as e:
        return f"读取失败: {e}"

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... (文件共 {len(content)} 字符，已截断)"

    return f"文件内容 ({path}, {len(content)} 字符):\n\n{content}"


def _filesystem_list(path: str) -> str:
    """列出目录内容."""
    if not path:
        path = "."

    p = Path(path)
    if not p.exists():
        return f"目录不存在: {path}"
    if not p.is_dir():
        return f"'{path}' 不是目录"

    items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    if not items:
        return f"目录 '{path}' 为空"

    lines = [f"目录 '{path}' 内容:"]
    for item in items[:50]:
        icon = "📁" if item.is_dir() else "📄"
        size = ""
        if item.is_file():
            try:
                sz = item.stat().st_size
                if sz > 1024 * 1024:
                    size = f" ({sz // (1024*1024)}MB)"
                elif sz > 1024:
                    size = f" ({sz // 1024}KB)"
                else:
                    size = f" ({sz}B)"
            except Exception:
                pass
        lines.append(f"  {icon} {item.name}{size}")

    if len(items) > 50:
        lines.append(f"  ... 还有 {len(items) - 50} 个项目")

    return "\n".join(lines)


def _clean_html(text: str) -> str:
    """去除 HTML 标签."""
    return re.sub(r'<[^>]+>', '', text).strip()
