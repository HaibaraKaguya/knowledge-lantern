"""Skill 系统 — 四层架构.

Layer 1: types.py   — Skill 数据类
Layer 2: parser.py  — SKILL.md → Skill 对象
Layer 3: loader.py  — 扫描 skills/ 目录 → list[Skill]
Layer 4: prompt.py  — Skill 列表 → 系统提示词 XML
"""
from src.skills.types import Skill, SkillCategory
from src.skills.parser import parse_skill_file
from src.skills.loader import load_skills
from src.skills.prompt import build_skill_prompt, get_skill_names

__all__ = [
    "Skill", "SkillCategory",
    "parse_skill_file",
    "load_skills",
    "build_skill_prompt", "get_skill_names",
]
