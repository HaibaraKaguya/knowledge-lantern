"""用户宪法加载器 — 读取偏好 → 生成 prompt 片段.

用法:
    from src.constitution import load_constitution, constitution_prompt
    prompt = constitution_prompt()
    # → "## 用户偏好\n- 前端: React + TypeScript\n..."
"""

from __future__ import annotations
import os
from pathlib import Path
import yaml

CONSTITUTION_PATH = os.path.expanduser("~/.shuchiin/constitution.yaml")

_DEFAULT = {
    "preferences": {
        "tech_stack": {"frontend": "", "backend": "", "database": ""},
        "code_style": {"language": "中文", "detail_level": "标准"},
        "output": {"explain_after": False, "show_code_only": False},
    },
    "current_project": {"name": "", "description": "", "directory": ""},
}


def load_constitution() -> dict:
    """加载用户宪法文件，不存在则返回空默认."""
    path = Path(CONSTITUTION_PATH)
    if not path.exists():
        return _DEFAULT
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return _DEFAULT
        # 合并默认值，防止缺少字段
        merged = dict(_DEFAULT)
        _deep_update(merged, data)
        return merged
    except Exception:
        return _DEFAULT


def _deep_update(base: dict, override: dict):
    """递归合并 override 到 base."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def constitution_prompt() -> str:
    """将宪法转换为注入 CEO prompt 的文本片段.

    返回空字符串表示没有有效偏好，避免不必要地占用 prompt 空间.
    """
    c = load_constitution()
    prefs = c.get("preferences", {})
    project = c.get("current_project", {})

    lines = []

    # ── 技术栈 ──
    tech = prefs.get("tech_stack", {})
    tech_parts = []
    if tech.get("frontend"):
        tech_parts.append(f"前端: {tech['frontend']}")
    if tech.get("backend"):
        tech_parts.append(f"后端: {tech['backend']}")
    if tech.get("database"):
        tech_parts.append(f"数据库: {tech['database']}")
    if tech_parts:
        lines.append("技术栈: " + " | ".join(tech_parts))

    # ── 代码风格 ──
    style = prefs.get("code_style", {})
    style_parts = []
    if style.get("language") and style["language"] != "中文":
        style_parts.append(f"{style['language']}注释")
    if style.get("detail_level") and style["detail_level"] != "标准":
        style_parts.append(f"{style['detail_level']}级别")
    if style_parts:
        lines.append("代码风格: " + " | ".join(style_parts))

    # ── 输出偏好 ──
    output = prefs.get("output", {})
    output_parts = []
    if output.get("explain_after"):
        output_parts.append("先给结果再解释")
    if output.get("show_code_only"):
        output_parts.append("只输出代码")
    if output_parts:
        lines.append("输出: " + " | ".join(output_parts))

    # ── 当前项目 ──
    proj_name = project.get("name", "")
    proj_desc = project.get("description", "")
    if proj_name:
        lines.append(f"当前项目: {proj_name}")
        if proj_desc:
            lines.append(f"项目描述: {proj_desc}")

    if not lines:
        return ""

    return "## 用户偏好宪法\n" + "\n".join(f"- {l}" for l in lines)
