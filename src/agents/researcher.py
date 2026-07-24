"""Researcher — 知识调研员（前端设计调研 + 通用知识调研）.

输入: {"question": "...", "role_prompt": "..."}
输出: {"final_answer": "...", "confidence": "HIGH|MEDIUM|LOW", "output_path": "..."}

身份：执行层 Consumer — 知识获取节点
Emoji：🧠

v2.1: 支持前端设计调研和通用知识调研两种模式，
      根据 role_prompt 内容自动切换。
"""

from __future__ import annotations
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from src.agents.llm import get_llm, safe_parse_json
from src.state.types import ResearcherState
from src.utils.files import save_json

RESEARCHER_PROMPT_FRONTEND = """\
# Researcher 前端设计调研员

**角色**：搬运设计知识 → 紧凑简报 → 保存到项目文件夹
**Emoji**：🧠

## 系统定位

在 Developer 编码之前，做前端设计调研。输出**紧凑的设计简报**（< 5000 字符），不要长篇代码。
Developer 自己有 CSS/HTML 能力，只需要设计方向、配色方案、参考链接。

## 调研维度（紧凑输出）

### 1. 设计方向（1句话）
- 现代简约 / Material Design / 新拟态 / 玻璃态？选一个

### 2. 配色方案（CSS变量，直接复制用）
```css
:root{--primary:#xxx;--bg:#xxx;--text:#xxx;--accent:#xxx}
```

### 3. 布局模式（1-2句话）
- 侧边栏+主内容 / 顶部导航+卡片网格 / 单页滚动？

### 4. 组件要点（每个2-3句话，关键CSS属性）
- 表格：条纹行/hover/响应式怎么做
- 表单：标签位置/输入框样式/验证状态
- 弹窗：居中方式/遮罩/动画
- 统计卡片：图标+数字+标签布局

### 5. 参考链接（2-3个URL，不复制内容）

### 6. 关键代码片段（1-2个，每个 < 1000 字符）

## 输出 JSON Schema
{
  "design_direction": "现代简约风格，蓝白主色调，卡片式布局",
  "css_variables": ":root{--primary:#3b82f6;...}",
  "layout_pattern": "顶部固定导航 + 主内容区卡片网格",
  "component_notes": "表格:斑马纹+sticky表头...",
  "reference_urls": ["https://..."],
  "code_snippets": ["/* 卡片样式 */ ..."],
  "file_plan": ["index.html","css/base.css","js/store.js",...]
}

## 禁止行为
- ❌ 长篇代码（> 2000 字符的片段不要）
- ❌ 泛泛而谈不写 CSS 变量
- ❌ 输出超过 5000 字符
"""

RESEARCHER_PROMPT_GENERAL = """\
# Researcher 通用知识调研员

**角色**：知识整合节点 — 多角度搜索、验证、整合信息
**Emoji**：🧠

## 系统定位

对用户问题进行深入调研，产出结构化的知识简报。
不做代码开发、不做设计图、只做信息整合。

## 调研维度

1. **核心概念**：主题的关键定义、背景、上下文
2. **关键事实**：具体的数据、时间线、版本、特性列表
3. **多方视角**：不同来源的观点对比、优劣势
4. **实践案例**：真实应用示例、代码片段（如有）
5. **参考来源**：可靠的信息来源链接

## 输出 JSON Schema
{
  "topic": "调研主题",
  "summary": "200字内核心总结",
  "key_findings": ["发现1", "发现2", "发现3"],
  "details": "详细调研内容（Markdown格式，含小标题）",
  "references": ["来源1", "来源2"]
}

## 禁止行为
- ❌ 编造不存在的数据和来源
- ❌ 输出不可靠/未经验证的信息
- ❌ 在不必要时写长篇代码
"""


def _research(state: ResearcherState, llm=None) -> dict:
    llm = llm or get_llm(0.3)
    role = state.get("role_prompt", "")
    q = state.get("question", "")

    # 根据 role 选择 prompt
    is_frontend = "前端" in role
    system_prompt = RESEARCHER_PROMPT_FRONTEND if is_frontend else RESEARCHER_PROMPT_GENERAL

    # ── 工具系统（仅通用调研模式）──
    if not is_frontend:
        from src.tools import get_registry, execute_tool
        registry = get_registry()
        # 初始加载 web_search 和 read_page
        tool_prompt = registry.inject_prompt(["web_search", "read_page"])
        tool_prompt += "\n" + registry.tool_search_prompt()
        system_prompt += "\n\n" + tool_prompt

    messages = [SystemMessage(content=system_prompt), HumanMessage(content=f"请研究以下内容：\n{q}")]
    resp = llm.invoke(messages).content

    # ── 工具调用循环（最多5轮）──
    if not is_frontend:
        for _round in range(5):
            tool_call = _parse_tool_call(resp)
            if not tool_call:
                break  # 最终回答，不是工具调用

            if tool_call.get("tool_search"):
                # 搜索工具
                kw = tool_call["tool_search"]
                found = registry.search(kw, limit=5)
                found_names = [t.name for t in found]
                tool_prompt = registry.inject_prompt(found_names) if found else "未找到匹配工具，请用现有工具完成任务。"
                print(f"     🔧 Researcher tool_search('{kw}') → {found_names}")
                messages.append(HumanMessage(content=f"找到以下工具:\n{tool_prompt}\n请选择合适工具继续。"))
                resp = llm.invoke(messages).content
                continue

            if tool_call.get("tool_call"):
                # 执行工具
                tool_name = tool_call["tool_call"]
                args = tool_call.get("arguments", {})
                result = execute_tool(tool_name, args)
                print(f"     🔧 Researcher tool_call({tool_name}, {list(args.keys())}) → {len(result)} 字符")
                messages.append(HumanMessage(content=f"工具 {tool_name} 返回:\n{result[:4000]}\n\n请基于以上结果继续完成调研。"))
                resp = llm.invoke(messages).content
                continue

        # 不再有工具调用了，resp 就是最终答案

    if is_frontend:
        result = safe_parse_json(resp, {
            "question": q,
            "design_direction": "现代简约风格",
            "css_variables": ":root{}",
            "layout_pattern": "顶部导航+卡片网格",
            "component_notes": "",
            "reference_urls": [],
            "code_snippets": [],
            "file_plan": ["index.html", "css/base.css", "js/store.js", "js/table.js",
                          "js/form.js", "js/search.js", "js/stats.js", "js/main.js"],
        })

        answer_parts = [
            f"## 设计方向\n{result.get('design_direction', '')}",
            f"## 配色方案\n```css\n{result.get('css_variables', '')}\n```",
            f"## 布局模式\n{result.get('layout_pattern', '')}",
            f"## 组件要点\n{result.get('component_notes', '')}",
            f"## 参考链接\n" + "\n".join(f"- {u}" for u in result.get('reference_urls', [])),
            f"## 代码片段\n" + "\n".join(f"```css\n{s}\n```" for s in result.get('code_snippets', [])),
        ]
        full_answer = "\n\n".join(answer_parts)
    else:
        result = safe_parse_json(resp, {
            "topic": q, "summary": resp[:200] if resp else "",
            "key_findings": [], "details": resp or "",
            "references": [],
        })
        full_answer = f"""## {result.get('topic', q)}

### 核心总结
{result.get('summary', '')}

### 关键发现
{chr(10).join(f'- {f}' for f in result.get('key_findings', []) if f)}

### 详细分析
{result.get('details', '')}

### 参考来源
{chr(10).join(f'- {r}' for r in result.get('references', []) if r)}
"""

    state["final_answer"] = full_answer
    state["confidence"] = "HIGH"
    print(f"     🧠 Researcher 完成 ({'设计简报' if is_frontend else '调研报告'}:{len(full_answer)} 字符)")
    return state


def _parse_tool_call(text: str) -> dict | None:
    """从 LLM 输出中提取 tool_call 或 tool_search JSON。不是工具调用则返回 None."""
    import re as _re
    # 尝试匹配 {"tool_call": ...} 或 {"tool_search": ...}
    m = _re.search(r'\{"tool_(?:call|search)"\s*:', text)
    if not m:
        return None
    try:
        return safe_parse_json(text, {})
    except Exception:
        return None


def _save(state: ResearcherState) -> dict:
    data = {
        "question": state.get("question", ""),
        "answer": state.get("final_answer", ""),
        "confidence": state.get("confidence", "MEDIUM"),
    }
    path = save_json("research", "res", "R", data)
    state["output_path"] = path
    print(f"     💾 调研保存到: {path}")
    return state


def build_researcher_subgraph(llm=None) -> StateGraph:
    llm = llm or get_llm()
    g = StateGraph(ResearcherState)
    g.add_node("research", lambda s: _research(s, llm))
    g.add_node("save", _save)
    g.set_entry_point("research")
    g.add_edge("research", "save")
    g.add_edge("save", END)
    return g.compile()
