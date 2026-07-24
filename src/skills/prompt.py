"""Layer 4: Skill 提示词注入 — list[Skill] → 拼入系统提示词的 XML 字符串."""

from __future__ import annotations
from src.skills.types import Skill


def build_skill_prompt(skills: list[Skill]) -> str:
    """将 skill 列表转换为注入到系统提示词的 XML 格式字符串.

    只包含 name + description（轻量索引），不包含完整 SKILL.md 正文。
    完整指令通过 load_skill() 按需加载。
    """
    if not skills:
        return ""

    lines = ["<skill_index>"]
    for s in skills:
        lines.append(f'  <skill name="{s.name}" category="{s.category.value}">')
        lines.append(f"    {s.description}")
        lines.append(f"  </skill>")
    lines.append("</skill_index>")

    return "\n".join(lines)


def get_skill_names(skills: list[Skill]) -> list[str]:
    """返回排序后的 skill 名称列表（供前端 / 补全使用）."""
    return sorted(s.name for s in skills)
