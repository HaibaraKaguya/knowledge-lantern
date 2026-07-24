"""沙箱模块 — 在隔离环境中执行 AI 生成代码.

用法:
    from src.sandbox import run_in_sandbox
    result = run_in_sandbox(code, content_type="code_node")
    # → {"passed": bool, "output": str, "error": str|None, ...}
"""

from src.sandbox.executor import run_in_sandbox

__all__ = ["run_in_sandbox"]
