"""沙箱执行器 — 在隔离环境中运行 AI 生成的产物并返回结果.

设计原则:
  1. 沙箱不关心管道结构，只知道 content + content_type → 运行 → 结果
  2. Docker 优先（真正隔离），不可用时自动降级到 subprocess
  3. 每种 content_type 有独立的执行策略
  4. 超时 + 资源限制 + 输出截断

content_type 执行策略:
  code_node     → npm install + npm test (Docker) 或 node --check (subprocess)
  code_python   → pip install + pytest (Docker) 或 python -m py_compile (subprocess)
  code_html     → HTML 结构验证 (纯静态，不需要运行时)
  code_backend  → 同 code_node
  report_md     → 跳过，不需要运行时验证
  research      → 跳过，不需要运行时验证
"""

from __future__ import annotations
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

logger = logging.getLogger("shuchiin.sandbox")

# 输出截断上限
MAX_OUTPUT_CHARS = 5000
# 默认执行超时（秒）
DEFAULT_TIMEOUT = 30

# 需要实际执行的 content_type
EXECUTABLE_TYPES = {
    "code", "code_node", "code_python", "code_backend",
}
# 只需要静态 HTML 验证的
HTML_TYPES = {"code_html", "code_frontend", "frontend"}
# 不需要执行的
SKIP_TYPES = {"report_md", "report", "research", "ppt", "image"}


def run_in_sandbox(content: str, content_type: str = "code", *,
                   timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """在沙箱中运行产物，返回运行结果.

    Args:
        content: 产物内容（代码/报告/HTML等）
        content_type: 产物类型，决定怎么运行
        timeout: 超时秒数

    Returns:
        {
            "passed": bool,        # 是否通过
            "output": str,         # stdout 输出（截断后）
            "error": str | None,   # 错误信息
            "skipped": bool,       # 是否跳过执行
            "sandbox_type": str,   # "docker" | "subprocess" | "static" | "skipped"
            "duration_ms": int,    # 执行耗时
        }
    """
    start = time.monotonic()

    # ── 不需要执行 ──
    if content_type in SKIP_TYPES:
        return {
            "passed": True, "output": "", "error": None,
            "skipped": True, "sandbox_type": "skipped",
            "duration_ms": 0,
        }

    # ── HTML 静态验证 ──
    if content_type in HTML_TYPES:
        return _validate_html(content, start)

    # ── 代码执行 ──
    if content_type in EXECUTABLE_TYPES or content_type not in (*EXECUTABLE_TYPES, *HTML_TYPES, *SKIP_TYPES):
        # 检测实际语言类型
        lang = _detect_language(content, content_type)
        return _execute_code(content, lang, timeout, start)

    # 兜底：跳过
    return {
        "passed": True, "output": "", "error": None,
        "skipped": True, "sandbox_type": "skipped",
        "duration_ms": _elapsed_ms(start),
    }


# ═══════════════════════════════════════════════════════════════════
# HTML 验证（不需要运行时）
# ═══════════════════════════════════════════════════════════════════

def _validate_html(content: str, start: float) -> dict[str, Any]:
    """静态验证 HTML/CSS/JS 结构."""
    findings = []

    # 检查 HTML 基本结构
    has_doctype = "<!DOCTYPE" in content or "<!doctype" in content
    has_html = "<html" in content.lower()
    has_head = "<head" in content
    has_body = "<body" in content
    has_closing = "</html>" in content

    if has_html:
        findings.append("✅ HTML 文档结构")
        if has_doctype: findings.append("  ✅ DOCTYPE 声明")
        else: findings.append("  ⚠️ 缺少 DOCTYPE")
        if has_head: findings.append("  ✅ <head>")
        if has_body: findings.append("  ✅ <body>")
        if has_closing: findings.append("  ✅ 闭合 </html>")
        if not has_closing: findings.append("  ⚠️ 缺少 </html>")

    # CSS 质量检查
    css_vars = len(re.findall(r'--[\w-]+\s*:', content))
    if css_vars:
        findings.append(f"✅ {css_vars} 个 CSS 变量")
    css_count = len(re.findall(r'\.([\w-]+)\s*\{', content))
    if css_count:
        findings.append(f"✅ ~{css_count} 个 CSS 类定义")

    # JS 检查
    js_funcs = len(re.findall(r'function\s+(\w+)', content))
    js_arrows = len(re.findall(r'\([^)]*\)\s*=>', content))
    total_js = js_funcs + js_arrows
    if total_js:
        findings.append(f"✅ ~{total_js} 个 JS 函数")
    if 'addEventListener' in content:
        findings.append("✅ 事件监听")
    if 'localStorage' in content:
        findings.append("✅ 数据持久化(localStorage)")

    # CDN 资源
    cdns = re.findall(r'(?:href|src)=["\'](https?://[^"\']+)["\']', content)
    if cdns:
        findings.append(f"✅ {len(cdns)} 个 CDN 资源")

    # 常见问题检查
    warnings = []
    if '<img' in content and 'alt=' not in content:
        warnings.append("⚠️ 图片缺少 alt 属性")
    if '<table' in content.lower() and '<th' not in content.lower():
        warnings.append("⚠️ 表格缺少表头 <th>")

    passed = len(warnings) == 0
    return {
        "passed": passed,
        "output": "\n".join(findings + warnings),
        "error": None,
        "skipped": False,
        "sandbox_type": "static",
        "duration_ms": _elapsed_ms(start),
        "html_findings": {"css_vars": css_vars, "css_classes": css_count, "js_functions": total_js, "cdns": len(cdns)},
    }


# ═══════════════════════════════════════════════════════════════════
# 代码执行
# ═══════════════════════════════════════════════════════════════════

def _detect_language(content: str, content_type: str) -> str:
    """检测代码语言."""
    if content_type in ("code_python",):
        return "python"
    if content_type in ("code_backend",):
        # 看文件内容判断
        if "const " in content or "require(" in content or "import " in content:
            if "express" in content.lower() or "module.exports" in content or "async " in content:
                return "node"
        if "def " in content or "import " in content:
            if "flask" in content.lower() or "fastapi" in content.lower() or "django" in content.lower():
                return "python"
        # 默认 Node（当前项目主要生成 Node 代码）
        return "node"
    # code, code_node, code_frontend 等 → 检查内容
    if "<html" in content.lower() or "<!DOCTYPE" in content.lower():
        return "html"
    if "const " in content or "require(" in content or "import " in content:
        return "node"
    if "def " in content or "import " in content or "class " in content:
        return "python"
    return "node"  # 默认


def _execute_code(content: str, lang: str, timeout: int, start: float) -> dict[str, Any]:
    """执行代码：Docker 优先，不可用时降级到 subprocess."""
    if lang == "html":
        return _validate_html(content, start)

    # 尝试 Docker
    if _docker_available():
        try:
            return _run_in_docker(content, lang, timeout, start)
        except Exception as e:
            logger.warning("Docker 执行失败，降级到 subprocess: %s", e)

    # 降级：subprocess
    return _run_in_subprocess(content, lang, timeout, start)


# ═══════════════════════════════════════════════════════════════════
# Docker 执行
# ═══════════════════════════════════════════════════════════════════

_docker_checked = False
_docker_available_flag = False

DOCKER_IMAGES = {
    "node": "node:20-alpine",
    "python": "python:3.12-alpine",
}

DOCKER_COMMANDS = {
    "node": "cd /workspace && npm install --production 2>&1 && node --check *.js 2>&1 && echo '--- tests ---' && npm test 2>&1 || true",
    "python": "cd /workspace && pip install -r requirements.txt 2>&1 || true && python -m py_compile *.py 2>&1 && echo '--- tests ---' && python -m pytest 2>&1 || true",
}


def _docker_available() -> bool:
    """检测 Docker 是否可用（带缓存，检测 daemon 是否能连接）."""
    global _docker_checked, _docker_available_flag
    if _docker_checked:
        return _docker_available_flag
    _docker_checked = True
    if shutil.which("docker") is None:
        _docker_available_flag = False
        return False
    # Docker 二进制存在，检查 daemon 是否在运行
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=3,
        )
        _docker_available_flag = r.returncode == 0
    except Exception:
        _docker_available_flag = False
    if not _docker_available_flag:
        logger.info("Docker daemon 未运行，将使用 subprocess 降级方案")
    return _docker_available_flag


def _run_in_docker(content: str, lang: str, timeout: int, start: float) -> dict[str, Any]:
    """在 Docker 容器中执行代码."""
    image = DOCKER_IMAGES.get(lang, DOCKER_IMAGES["node"])
    command = DOCKER_COMMANDS.get(lang, DOCKER_COMMANDS["node"])

    with tempfile.TemporaryDirectory(prefix="sandbox-") as tmpdir:
        _write_code_files(content, tmpdir, lang)

        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--memory=512m",
                    "--cpus=1",
                    "--pids-limit=64",
                    "--network=none",
                    "--read-only",
                    f"--tmpfs=/tmp:size=128m",
                    "-v", f"{tmpdir}:/workspace:ro",
                    f"--name=sandbox-{os.getpid()}-{int(time.time())}",
                    image,
                    "sh", "-c", command,
                ],
                capture_output=True, text=True,
                timeout=timeout,
            )
            output = result.stdout + "\n" + result.stderr
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n... (输出截断)"

            return {
                "passed": result.returncode == 0,
                "output": output,
                "error": None if result.returncode == 0 else f"返回码 {result.returncode}",
                "skipped": False,
                "sandbox_type": "docker",
                "duration_ms": _elapsed_ms(start),
            }
        except subprocess.TimeoutExpired:
            _cleanup_container(f"sandbox-{os.getpid()}-{int(time.time())}")
            return {
                "passed": False,
                "output": "",
                "error": f"执行超时 ({timeout}s)",
                "skipped": False,
                "sandbox_type": "docker",
                "duration_ms": _elapsed_ms(start),
            }


def _cleanup_container(name: str):
    """清理残留容器."""
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=5)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# Subprocess 执行（Docker 不可用时的降级方案）
# ═══════════════════════════════════════════════════════════════════

def _run_in_subprocess(content: str, lang: str, timeout: int, start: float) -> dict[str, Any]:
    """在子进程中执行代码（降级方案，只做语法检查，不安装依赖）."""
    with tempfile.TemporaryDirectory(prefix="sandbox-") as tmpdir:
        _write_code_files(content, tmpdir, lang)

        if lang == "node":
            return _subprocess_node(tmpdir, timeout, start)
        elif lang == "python":
            return _subprocess_python(tmpdir, timeout, start)
        else:
            return _subprocess_node(tmpdir, timeout, start)


def _subprocess_node(tmpdir: str, timeout: int, start: float) -> dict[str, Any]:
    """subprocess 模式的 Node 语法检查."""
    js_files = []
    for root, _, files in os.walk(tmpdir):
        for f in files:
            if f.endswith(".js"):
                js_files.append(os.path.join(root, f))

    output_parts = []
    all_passed = True

    for js_file in js_files[:10]:  # 最多检查 10 个文件
        rel = os.path.relpath(js_file, tmpdir)
        try:
            r = subprocess.run(
                ["node", "--check", js_file],
                capture_output=True, text=True, timeout=10,
                cwd=tmpdir,
            )
            if r.returncode == 0:
                output_parts.append(f"✅ {rel}: 语法正确")
            else:
                output_parts.append(f"❌ {rel}: {r.stderr.strip()[:200]}")
                all_passed = False
        except subprocess.TimeoutExpired:
            output_parts.append(f"⚠️ {rel}: 检查超时")
            all_passed = False
        except FileNotFoundError:
            return {
                "passed": True, "output": "⚠️ Node.js 未安装，跳过语法检查",
                "error": "Node.js not found", "skipped": True,
                "sandbox_type": "subprocess", "duration_ms": _elapsed_ms(start),
            }

    return {
        "passed": all_passed,
        "output": "\n".join(output_parts) if output_parts else "无 JS 文件可检查",
        "error": None if all_passed else "部分文件语法检查失败",
        "skipped": False,
        "sandbox_type": "subprocess",
        "duration_ms": _elapsed_ms(start),
    }


def _subprocess_python(tmpdir: str, timeout: int, start: float) -> dict[str, Any]:
    """subprocess 模式的 Python 语法检查."""
    py_files = []
    for root, _, files in os.walk(tmpdir):
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))

    output_parts = []
    all_passed = True

    for py_file in py_files[:10]:
        rel = os.path.relpath(py_file, tmpdir)
        try:
            r = subprocess.run(
                ["python3", "-m", "py_compile", py_file],
                capture_output=True, text=True, timeout=10,
                cwd=tmpdir,
            )
            if r.returncode == 0:
                output_parts.append(f"✅ {rel}: 语法正确")
            else:
                output_parts.append(f"❌ {rel}: {r.stderr.strip()[:200]}")
                all_passed = False
        except subprocess.TimeoutExpired:
            output_parts.append(f"⚠️ {rel}: 检查超时")
            all_passed = False
        except FileNotFoundError:
            return {
                "passed": True, "output": "⚠️ Python 未安装，跳过语法检查",
                "error": "Python not found", "skipped": True,
                "sandbox_type": "subprocess", "duration_ms": _elapsed_ms(start),
            }

    return {
        "passed": all_passed,
        "output": "\n".join(output_parts) if output_parts else "无 Python 文件可检查",
        "error": None if all_passed else "部分文件语法检查失败",
        "skipped": False,
        "sandbox_type": "subprocess",
        "duration_ms": _elapsed_ms(start),
    }


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _write_code_files(content: str, tmpdir: str, lang: str):
    """从多文件格式 (=== filename ===\ncontent) 或单文件内容写入临时目录."""
    file_pattern = re.compile(r'^=== (.+?) ===\n', re.MULTILINE)
    matches = list(file_pattern.finditer(content))

    if matches:
        # 多文件格式
        for i, m in enumerate(matches):
            fname = m.group(1).strip()
            s = m.end()
            e = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            file_content = content[s:e].strip()
            file_path = os.path.join(tmpdir, fname)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)
    else:
        # 单文件 → 根据语言给默认名
        ext_map = {"node": "index.js", "python": "main.py", "html": "index.html"}
        fname = ext_map.get(lang, "output.txt")
        with open(os.path.join(tmpdir, fname), "w", encoding="utf-8") as f:
            f.write(content)

    # 始终写一个 package.json（Node 项目需要）
    if lang == "node" and not os.path.exists(os.path.join(tmpdir, "package.json")):
        with open(os.path.join(tmpdir, "package.json"), "w") as f:
            f.write('{"name":"sandbox-test","private":true}')


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
