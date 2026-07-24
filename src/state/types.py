"""状态类型定义 — 简化线性管道版.

信息封锁规则：
  每个 Agent 只能看到自己需要的字段，通过 input/output 适配器实现。
  父图 state 存所有产出，但 Agent 子图只看到自己的输入字段。

Reducer:
  _merge_ledger — 列表按 ID 去重合并
  _last         — 取最后写入的值
"""

from __future__ import annotations
from typing import Annotated, TypedDict


def _merge_ledger(a: list | None, b: list | None) -> list:
    """按 id 合并 task_ledger——同 id 替换，不追加重复条目."""
    if a is None: return b or []
    if b is None: return a
    merged = {}
    for t in a:
        if t.get("id"): merged[t["id"]] = t
    for t in b:
        if t.get("id"): merged[t["id"]] = t
    return list(merged.values())


def _last(a, b): return b if b is not None else a


def _merge_deep(a: dict | None, b: dict | None) -> dict:
    """Deep-merge two dicts by role key. b overrides a at leaf keys."""
    if a is None: return b or {}
    if b is None: return a
    merged = dict(a)
    for k, v in b.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


# ═══════════════════════════════════════════════════════════════════
# 父图状态（所有 Agent 产出的聚合）
# ═══════════════════════════════════════════════════════════════════

class TaskEntry(TypedDict):
    id: str
    description: str
    assignee: str
    role: str
    status: str
    result_ref: str
    summary: str


def _merge_dispatch_tasks(a: dict | None, b: dict | None) -> dict:
    """按 key 合并 dispatch_tasks，不互相覆盖."""
    if a is None: return b or {}
    if b is None: return a
    merged = dict(a)
    for k, v in b.items():
        if v is not None: merged[k] = v
        elif k in merged: del merged[k]
    return merged


class ParentState(TypedDict):
    # ── 输入 ──
    user_input: Annotated[str | None, _last]
    thread_id: Annotated[str | None, _last]

    # ── CEO 产出：分角色任务槽位 ──
    dispatch_tasks: Annotated[dict | None, _merge_dispatch_tasks]
    dispatch_task: Annotated[dict | None, _last]      # 当前分支任务
    dispatch_context: Annotated[str | None, _last]     # Researcher 产出路径

    # ── Developer 产出（role-specific，前后端不互相覆盖）──
    code: Annotated[str, _last]
    code_frontend: Annotated[str, _last]
    code_backend: Annotated[str, _last]
    output_path: Annotated[str, _last]
    output_path_frontend: Annotated[str, _last]
    output_path_backend: Annotated[str, _last]
    html_url: Annotated[str, _last]
    html_url_frontend: Annotated[str, _last]
    html_url_backend: Annotated[str, _last]

    # ── Reviewer 产出（role-specific）──
    review_report: Annotated[dict | None, _last]
    review_report_frontend: Annotated[dict | None, _last]
    review_report_backend: Annotated[dict | None, _last]
    approved: Annotated[bool, _last]
    approved_frontend: Annotated[bool, _last]
    approved_backend: Annotated[bool, _last]

    # ── 退回重做时的增量修复字段（role-specific）──
    kept_code: Annotated[str, _last]
    kept_code_frontend: Annotated[str, _last]
    kept_code_backend: Annotated[str, _last]
    reviewer_feedback: Annotated[str, _last]
    reviewer_feedback_frontend: Annotated[str, _last]
    reviewer_feedback_backend: Annotated[str, _last]
    retry_count_frontend: Annotated[int, _last]
    retry_count_backend: Annotated[int, _last]
    research_question: Annotated[str, _last]

    # ── Developer 子图事件（文件计划 + 进度，向上传递到前端）──
    dev_events: Annotated[list[dict], lambda a, b: (a or []) + (b or [])]

    # ── 问题笔记本（Reviewer→Researcher→Developer 共享）──
    notebook: Annotated[dict | None, _last]  # {filename: {issues, keywords, fix_status}}

    # ── 动态角色通用产出 ──
    # {role: {content, content_type, output_path, html_url, review_report, approved,
    #          kept_content, reviewer_feedback, retry_count, notebook}}
    outputs: Annotated[dict[str, dict], _merge_deep]
    task_type: Annotated[str, _last]  # "code" | "research" | "analysis" | "report"

    # ── 共享账本 ──
    task_ledger: Annotated[list[dict], _merge_ledger]
    messages: Annotated[list, lambda a, b: (a or []) + (b or [])]


# ═══════════════════════════════════════════════════════════════════
# Developer 子图私有状态
# ═══════════════════════════════════════════════════════════════════

class DeveloperState(TypedDict):
    task_description: str
    role_prompt: str
    research_notes: str
    retry_feedback: str
    draft_code: str
    certainty_annotations: list[dict]
    final_code: str
    self_check_passed: bool
    self_check_topics: str
    output_path: str
    html_url: str
    # 循环生成控制字段
    _file_queue: list[str]       # 待生成文件列表
    _current_idx: int            # 当前文件索引
    _generated_files: dict       # {filename: content} 已生成的文件
    _events: list[dict]          # 事件列表（file_plan, file_done）向上传递到前端
    _notebook: dict              # 问题笔记本 {filename: {"issues": [...], "fix_status": "pending|done"}}


# ═══════════════════════════════════════════════════════════════════
# Researcher 子图私有状态
# ═══════════════════════════════════════════════════════════════════

class ResearcherState(TypedDict):
    question: str
    role_prompt: str
    draft_notes: str
    final_answer: str
    confidence: str
    output_path: str
