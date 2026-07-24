"""Skill 类型定义 — Layer 1 数据模型."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SkillCategory(StrEnum):
    PUBLIC = "public"
    CUSTOM = "custom"


@dataclass
class Skill:
    """一个 Skill 的完整元数据."""
    name: str
    description: str
    skill_dir: Path          # skill 目录绝对路径
    skill_file: Path         # SKILL.md 文件绝对路径
    category: SkillCategory  # public / custom
    enabled: bool = True
    # 执行元数据（可选）
    executor: str = "prompt"   # "prompt" | "script" — 执行方式
    runtime: str = ""          # "python" | "node" | "bash" — 脚本运行时
    entrypoint: str = ""       # "scripts/generate.py" — 相对于 skill_dir 的脚本路径

    @property
    def prompt_instructions(self) -> str:
        """读取 SKILL.md 中 YAML 头之后的 Markdown 指令正文."""
        text = self.skill_file.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
        return text.strip()

    @property
    def script_path(self) -> Path | None:
        """脚本的绝对路径，无脚本返回 None."""
        if not self.entrypoint:
            return None
        return self.skill_dir / self.entrypoint
