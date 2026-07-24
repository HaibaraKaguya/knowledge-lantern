"""Layer 2: SKILL.md 解析器 — YAML frontmatter → Skill 对象."""

from __future__ import annotations
import re
from pathlib import Path
import yaml

from src.skills.types import Skill, SkillCategory


def parse_skill_file(skill_file: Path, category: SkillCategory = SkillCategory.PUBLIC) -> Skill | None:
    """解析单个 SKILL.md 文件，返回 Skill 对象。失败返回 None."""
    if not skill_file.exists():
        return None

    text = skill_file.read_text(encoding="utf-8")

    # 提取 --- 之间的 YAML frontmatter
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None

    try:
        frontmatter = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")

    if not name or not description:
        return None

    # 提取执行元数据（可选）
    executor = frontmatter.get("executor", "prompt")
    runtime = frontmatter.get("runtime", "")
    entrypoint = frontmatter.get("entrypoint", "")

    # 如果声明了 script executor 但没指定 runtime，从 entrypoint 后缀推断
    if executor == "script" and not runtime and entrypoint:
        if entrypoint.endswith(".py"):
            runtime = "python"
        elif entrypoint.endswith(".js"):
            runtime = "node"
        elif entrypoint.endswith(".sh"):
            runtime = "bash"

    return Skill(
        name=name,
        description=description,
        skill_dir=skill_file.parent,
        skill_file=skill_file,
        category=category,
        enabled=True,
        executor=executor,
        runtime=runtime,
        entrypoint=entrypoint,
    )
