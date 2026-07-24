"""CEO — 任务分类与 Agent 调度.

输入: {"user_input": "..."}
输出: {"task_type": "code|research|analysis|report", "dispatch_tasks": {...}}

身份：治理层 Producer — 战略拆解节点
Emoji：🎯

v2.1: 支持 code / research / analysis / report 四种任务类型，
      不再只拆为 frontend+backend。
"""

from __future__ import annotations
from langchain_core.messages import HumanMessage, SystemMessage
from src.agents.llm import get_llm, safe_parse_json
from src.constitution import constitution_prompt
from src.skills import load_skills, build_skill_prompt


def _default_assignee(role: str) -> str:
    """Map role → default agent type."""
    return {
        "frontend": "developer", "backend": "developer",
        "research": "researcher", "report": "writer",
        "data_analysis": "developer",
    }.get(role, "developer")


CEO_ANALYZE_PROMPT = """\
# CEO 任务分析官

**角色**：治理层 Producer — 战略拆解节点
**Emoji**：🎯

## 系统定位

接收用户自然语言需求，先分类再拆解为子任务。
子任务通过并行管道执行——互不阻塞。

## 第一步：任务分类

| 类型 | 关键词/场景 | 含义 |
|------|-----------|------|
| **code** | 游戏/计算器/工具/页面/网站/API/数据库/管理系统/博客/电商/后端/全栈 | 需要写代码的开发任务 |
| **research** | 调研/研究/搜索/查询/了解/解释/对比/报告/文档（纯文字产出） | 搜索信息+整合输出，不写代码 |
| **analysis** | 数据分析/统计/图表/可视化/CSV/Excel | 分析数据+产出图表 |
| **report** | PPT/幻灯片/演示/写报告/生成文档 | 产出文档/幻灯片 |

## 第二步：按类型拆解任务

### code 类型
- **纯前端项目**（游戏、计算器、工具页面、landing page）→ 只创建 frontend 任务
- **纯后端项目**（API服务、数据库设计）→ 只创建 backend 任务
- **全栈项目**（博客、电商、后台管理）→ 创建 frontend + backend

### research 类型
- 创建一个 research 任务，描述调研问题
- 不需要 developer/reviewer，结果直接返回用户

### analysis 类型
- 创建一个 data_analysis 任务，描述数据和需要分析的维度

### report 类型
- 创建一个 report 任务，描述报告主题和结构要求

## 输出 JSON
{
  "analysis": "一句话分析",
  "task_type": "code",
  "tasks": {
    "frontend": {"id":"T001","description":"创建...","assignee":"developer","role":"frontend"},
    "backend": {"id":"T002","description":"创建API...","assignee":"developer","role":"backend"}
  }
}

注意：
- 只创建有意义的任务，不需要的 key 设为 null 或不填
- "做个打飞机游戏" → task_type="code", 只有 frontend
- "调研 React 19 新特性" → task_type="research", 只有 research
- "分析销售数据" → task_type="analysis", 只有 data_analysis
- "写一份市场分析报告" → task_type="report", 只有 report
"""


def analyze_and_dispatch(state: dict, llm=None) -> dict:
    """分析用户任务，产出 task_type + dispatch_tasks."""
    llm = llm or get_llm(0.3)
    user_input = state.get("user_input", "")

    # ── 快速路径：/skill-name 语法 → 直接路由到 SkillRunner ──
    import re as _re
    skill_match = _re.match(r'^/([\w-]+)\s*(.*)', user_input.strip())
    if skill_match:
        skill_name = skill_match.group(1)
        skill_args = skill_match.group(2) or user_input
        # 验证 skill 是否存在
        from src.skills import load_skills
        skills = load_skills()
        known = {s.name for s in skills}
        if skill_name in known:
            print(f"  🎯 CEO: /{skill_name} → SkillRunner")
            return {
                "dispatch_tasks": {
                    skill_name: {
                        "id": "T001", "description": skill_args,
                        "assignee": "skill_runner", "role": skill_name,
                        "status": "pending",
                    }
                },
                "task_type": "skill",
            }

    # ── 用户宪法注入 ──
    constitution = constitution_prompt()
    if constitution:
        constitution = constitution + "\n请遵守以上偏好。\n\n"

    # ── 技能注入 ──
    skill_prompt = ""
    try:
        skills = load_skills()
        sp = build_skill_prompt(skills)
        if sp:
            skill_prompt = sp + "\n\n"
    except Exception:
        pass

    full_system = constitution + skill_prompt + CEO_ANALYZE_PROMPT

    try:
        resp = llm.invoke([
            SystemMessage(content=full_system),
            HumanMessage(content=user_input)
        ]).content
        result = safe_parse_json(resp, {
            "analysis": user_input,
            "task_type": "code",
            "tasks": {
                "frontend": {"id": "T001", "description": user_input,
                              "assignee": "developer", "role": "frontend"},
            }
        })
    except Exception:
        result = {
            "analysis": user_input,
            "task_type": "code",
            "tasks": {
                "frontend": {"id": "T001", "description": user_input,
                              "assignee": "developer", "role": "frontend"},
            }
        }

    tasks = result.get("tasks", {})
    task_type = result.get("task_type", "code")
    dispatch = {}

    for role, t in tasks.items():
        if t and isinstance(t, dict):
            t.setdefault("status", "pending")
            t.setdefault("id", f"T00{len(dispatch)+1}")
            t.setdefault("assignee", _default_assignee(role))
            t.setdefault("role", role)
            dispatch[role] = t

    if not dispatch:
        dispatch["frontend"] = {"id": "T001", "description": user_input,
                                 "assignee": "developer", "role": "frontend", "status": "pending"}

    roles = list(dispatch.keys())
    print(f"  🎯 CEO 拆解: type={task_type}, {len(dispatch)} 个任务 ({', '.join(roles)})")
    for r, t in dispatch.items():
        print(f"     [{t['id']}] {r}: {t['description'][:80]}")

    return {"dispatch_tasks": dispatch, "task_type": task_type}
