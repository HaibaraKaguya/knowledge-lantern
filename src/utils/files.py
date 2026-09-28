"""项目路径常量。

旧多 Agent 工作流的 JSON 产出函数已随 agent 工作流一起删除；
本模块现在只保留跨包共享的 data 目录定位。
"""

from __future__ import annotations
from pathlib import Path

# 绝对路径：不依赖 CWD，无论从哪里启动都指向项目根目录的 data/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = str(_PROJECT_ROOT / "data")
