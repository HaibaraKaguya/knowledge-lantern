"""JSON 文件读写 — Agent 间通信的唯一媒介.

设计意图：
  Agent 不通过 state 字典传递原始内容（代码、调研结果），
  而是写入 JSON 文件，在 state 里只存文件路径。
  效果：
    1. 强制信息精简 — state 里是路径，不是内容，不会膨胀
    2. 注意力聚焦 — Agent 打开 JSON 文件只看到和自己相关的内容
    3. 可追溯 — 每个产出物有独立的 JSON 文件，事后可审计

核心函数:
  save_json  — 保存到 data/outputs/ 或 data/research/，返回相对路径
  load_json  — 读取 JSON 文件（调用者自行处理异常）
  summarize  — 从 JSON 文件提取摘要（不加载全文到 state）
  ensure_dirs— 确保 data/outputs/ 和 data/research/ 存在

安全注意事项：
  - task_id 直接拼入文件名，未做路径穿越检查
  - 生产环境应校验 task_id 不包含 "/"、".." 等特殊字符
  - 文件命名包含时间戳到秒，同一秒内的重复写入会覆盖
"""

from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
from pathlib import Path

# 绝对路径：不依赖 CWD，无论从哪里启动都指向项目根目录的 data/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = str(_PROJECT_ROOT / "data")
OUTPUTS_DIR = os.path.join(DATA_DIR, "outputs")
RESEARCH_DIR = os.path.join(DATA_DIR, "research")


def ensure_dirs():
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    os.makedirs(RESEARCH_DIR, exist_ok=True)


def save_json(subdir: str, prefix: str, task_id: str, data: dict) -> str:
    """保存 JSON 文件，返回路径。路径被存入 state，Agent 通过路径读取。

    Args:
        subdir: "outputs" | "research"
        prefix: "dev" | "res"
        task_id: "T001" — 已校验，不含 "/"、".." 等路径穿越字符
        data: 要保存的数据

    Returns: 如 "data/outputs/T001_dev_20260710T120000_123456.json"
    """
    # 路径穿越校验
    if not re.match(r'^[A-Za-z0-9_-]+$', task_id):
        raise ValueError(f"task_id 包含非法字符: {task_id!r}")
    if subdir not in ("outputs", "research"):
        raise ValueError(f"subdir 必须是 'outputs' 或 'research': {subdir!r}")

    ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    fname = f"{task_id}_{prefix}_{ts}.json"
    base = OUTPUTS_DIR if subdir == "outputs" else RESEARCH_DIR
    path = os.path.join(base, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_json(path: str) -> dict:
    """🔴 调用者必须自己处理 FileNotFoundError、JSONDecodeError 等异常。
    当前调用方（reviewer.py）用 except Exception: pass 静默吞掉了所有错误。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def summarize(path: str, max_chars: int = 200) -> str:
    """从 JSON 文件提取摘要（不加载全文到 state）。

    🟡 依赖固定的字段名（final_code, final_answer），
    如果产出格式变更，会回退到 str(data)[:200] 输出不可读的摘要。
    """
    try:
        data = load_json(path)
        # 提取关键字段做摘要
        if "final_code" in data:
            # 🟡 summary 字段可能不存在（Developer 产出不包含 summary 键）
            return f"[code:{len(data['final_code'])}chars] {data.get('summary','')[:max_chars]}"
        if "final_answer" in data:
            return f"[research:{data.get('confidence','?')}] {data['final_answer'][:max_chars]}"
        return str(data)[:max_chars]
    except Exception:
        return f"[无法读取:{path}]"
