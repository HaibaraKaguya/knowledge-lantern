"""Layer 3: Skill 扫描加载器 — os.walk → 找到所有 SKILL.md → 解析排序."""

from __future__ import annotations
import os
from pathlib import Path

from src.skills.parser import parse_skill_file
from src.skills.types import Skill, SkillCategory


def _find_skills_root() -> Path:
    """查找项目 skills/ 根目录."""
    here = Path(__file__).resolve()
    project_root = here.parent.parent.parent  # src/skills/loader.py → 项目根
    return project_root / "skills"


def _auto_detect_executor(skill: Skill) -> Skill:
    """如果 skill 有 scripts/ 目录但未声明 executor，自动检测."""
    if skill.executor != "prompt":
        return skill  # 已在 SKILL.md YAML 中声明

    scripts_dir = skill.skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return skill

    # 找到 scripts/ 下的第一个可执行文件
    script_files = list(scripts_dir.iterdir())
    if not script_files:
        return skill

    first = script_files[0]
    if not first.is_file():
        return skill

    # 根据后缀推断 runtime
    suffix = first.suffix.lower()
    runtime_map = {".py": "python", ".js": "node", ".sh": "bash"}
    runtime = runtime_map.get(suffix, "")

    if runtime:
        entrypoint = f"scripts/{first.name}"
        # 修改 Skill（需要改 frozen=False——已经在 types.py 中改为普通 dataclass）
        skill.executor = "script"
        skill.runtime = runtime
        skill.entrypoint = entrypoint

    return skill


def load_skills(skills_root: Path | None = None) -> list[Skill]:
    """扫描 skills/ 目录，加载所有 SKILL.md 文件.

    Args:
        skills_root: skills/ 目录路径，None 则自动查找项目下的 skills/

    Returns:
        按名称排序的 Skill 列表
    """
    if skills_root is None:
        skills_root = _find_skills_root()

    if not skills_root.is_dir():
        return []

    seen: dict[str, Skill] = {}  # 按 name 去重，public 优先

    for dirpath, _, filenames in os.walk(skills_root):
        if "SKILL.md" not in filenames:
            continue

        skill_path = Path(dirpath)
        skill_file = skill_path / "SKILL.md"

        # 判断 category：路径中带 public/ 的就是 public skill
        rel = skill_path.relative_to(skills_root)
        if rel.parts and rel.parts[0] in ("public", "custom"):
            category = SkillCategory(rel.parts[0])
        else:
            category = SkillCategory.PUBLIC

        skill = parse_skill_file(skill_file, category)
        if skill is None:
            continue

        # 自动检测可执行脚本
        skill = _auto_detect_executor(skill)

        # 去重：同 name 保留第一个（public 优先于 custom）
        if skill.name not in seen:
            seen[skill.name] = skill

    return sorted(seen.values(), key=lambda s: s.name)
