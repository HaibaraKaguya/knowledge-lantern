"""Writer — 文档/报告产出者.

子图: _plan → _write_sections → _save → END
复用 Developer 的并行生成模式，针对文档/报告优化。

输入: {"task_description": "...", "role_prompt": "...", "research_notes": "..."}
输出: {"final_code": "...", "output_path": "..."}

身份：执行层 Consumer — 文档产出节点
Emoji：📝
"""

from __future__ import annotations
import os as _os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from src.agents.llm import get_llm, safe_parse_json
from src.state.types import DeveloperState  # 复用 DeveloperState，字段足够通用
from src.utils.files import save_json, DATA_DIR

WRITER_PROMPTS = {
    "report": """你是专业报告撰写专家。产出结构化 Markdown 报告。
要求：
- 清晰的层级结构（h2/h3/h4）
- 关键数据用表格展示
- 结论和建议突出显示
- 专业的语气和术语
- 不写代码（除非是技术报告中的示例片段）
""",
    "ppt": """你是 PPT 大纲设计师。产出幻灯片结构。
要求：
- 每页一个核心观点
- 子弹点简洁有力
- 视觉化描述（图表、图片占位）
- 过渡自然
""",
    "general": """你是技术文档撰写者。产出清晰的技术文档。
要求：
- 结构完整（概述/详情/API/示例）
- 代码示例简洁准确
- 术语一致
""",
}

PLAN_SECTIONS_PROMPT = """\
你是报告规划师。将主题拆分为逻辑章节。

## 规则
- 每个章节 200-500 字
- 3-6 个章节
- 章节间逻辑递进

## 输出 JSON
{
  "sections": ["概述", "背景与现状", "关键分析", "案例研究", "结论与建议"]
}
"""

WRITE_SECTION_PROMPT = """\
{role_prompt}

## 报告主题
{task_description}

{research_context}

## 全部章节规划
{siblings}

## 当前要写的章节：{current_section}

## 规则
- 只写这一章节的内容
- 完整、具体、有数据支撑
- Markdown 格式，层级从 ## 开始
- 200-500 字
"""


def _plan(state: DeveloperState, llm=None) -> dict:
    """规划报告章节."""
    llm = llm or get_llm(0.2)
    task = state.get("task_description", "")

    try:
        resp = llm.invoke([
            SystemMessage(content=PLAN_SECTIONS_PROMPT),
            HumanMessage(content=f"主题：{task[:300]}")
        ]).content
        plan = safe_parse_json(resp, {
            "sections": ["概述", "主要内容", "总结"]
        })
    except Exception:
        plan = {"sections": ["概述", "主要内容", "总结"]}

    sections = plan.get("sections", ["概述", "主要内容", "总结"])
    if len(sections) < 2:
        sections = ["概述", "主要内容", "总结"]

    print(f"     📋 章节规划: {sections}")

    return {
        "_file_queue": sections,
        "_current_idx": 0,
        "_generated_files": {},
        "_events": [{"type": "file_plan", "files": sections, "total": len(sections)}],
    }


def _write_sections(state: DeveloperState, llm=None) -> dict:
    """并行生成各章节."""
    llm = llm or get_llm(0.4)

    queue = state.get("_file_queue", ["概述", "主要内容", "总结"])
    if not queue:
        return {"_generated_files": {}, "_current_idx": 0,
                "_events": list(state.get("_events", []))}

    role = state.get("role_prompt", WRITER_PROMPTS["general"])
    task = state.get("task_description", "")
    research = state.get("research_notes", "")

    max_workers = min(len(queue), int(_os.environ.get("DEV_PARALLEL_WORKERS", "3")))
    research_ctx = f"## 调研资料\n{research[:4000]}" if research else ""
    siblings = "\n".join(f"- {s}" for s in queue)

    print(f"     🚀 并行生成 {len(queue)} 个章节 (workers={max_workers})")

    generated = dict(state.get("_generated_files", {}))
    events: list[dict] = list(state.get("_events", []))
    lock = threading.Lock()
    completed_count = [0]

    def write_section(section_name: str) -> tuple[str, str]:
        thread_llm = get_llm(0.4)
        prompt = WRITE_SECTION_PROMPT.format(
            role_prompt=role,
            task_description=task[:500],
            research_context=research_ctx,
            siblings=siblings,
            current_section=section_name,
        )

        try:
            resp = thread_llm.invoke([
                SystemMessage(content=prompt),
                HumanMessage(content=f"撰写章节：{section_name}")
            ]).content
        except Exception as e:
            print(f"     ❌ {section_name} 生成失败: {e}")
            resp = f"*[{section_name} 生成失败: {e}]*"

        with lock:
            completed_count[0] += 1
            print(f"     📄 {section_name}: {len(resp)} 字符 ({completed_count[0]}/{len(queue)})")

        return (section_name, resp.strip())

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(write_section, s): s for s in queue}
        for future in as_completed(futures):
            name, content = future.result()
            generated[name] = content
            with lock:
                events.append({
                    "type": "file_done",
                    "file": name,
                    "chars": len(content),
                })

    return {
        "_generated_files": generated,
        "_current_idx": len(queue),
        "_events": events,
    }


def _save(state: DeveloperState) -> dict:
    """拼接所有章节为完整 Markdown 报告."""
    generated = state.get("_generated_files", {})
    events = list(state.get("_events", []))

    if not generated:
        return {"final_code": "", "output_path": "", "_events": events}

    # 按原始顺序拼接
    queue = state.get("_file_queue", list(generated.keys()))
    sections = []
    for s in queue:
        content = generated.get(s, "")
        if content:
            sections.append(f"## {s}\n\n{content}")

    full_report = "\n\n".join(sections)
    total_chars = sum(len(c) for c in generated.values())

    # 保存
    path = save_json("outputs", "writer", "W", {"content": full_report, "sections": generated})

    print(f"     💾 保存报告: {len(generated)} 章节, {total_chars} 字符")

    events.append({
        "type": "file_save",
        "total_chars": total_chars,
        "file_count": len(generated),
        "html_url": "",
    })

    return {
        "output_path": path,
        "final_code": full_report,
        "html_url": "",
        "_generated_files": generated,
        "_events": events,
    }


def build_writer_subgraph(llm=None) -> StateGraph:
    """构建 Writer 子图：_plan → _write_sections → _save → END."""
    llm = llm or get_llm()
    g = StateGraph(DeveloperState)  # 复用 DeveloperState TypedDict
    g.add_node("plan", lambda s: _plan(s, llm))
    g.add_node("write_sections", lambda s: _write_sections(s, llm))
    g.add_node("save", _save)
    g.set_entry_point("plan")
    g.add_edge("plan", "write_sections")
    g.add_edge("write_sections", "save")
    g.add_edge("save", END)
    return g.compile()
