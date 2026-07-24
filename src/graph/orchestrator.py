
"""
Orchestrator — 并行 Developer/Writer + Researcher 前置调研 + 3次重试 fallback.

v2.1: 支持 code / research / analysis / report 四种任务类型，
      动态路由到 Developer（代码）或 Writer（文档）管道。
"""

from __future__ import annotations
import asyncio
import logging, os, sqlite3, time, traceback as _tb
from langgraph.graph import END, StateGraph
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Send

from src.agents.llm import get_llm
from src.agents.ceo import analyze_and_dispatch
from src.agents.developer import build_developer_subgraph, _build_notebook
from src.agents.researcher import build_researcher_subgraph
from src.agents.reviewer import execute as reviewer_execute
from src.agents.writer import build_writer_subgraph
from src.agents.skill_runner import build_skill_runner_subgraph
from src.sandbox import run_in_sandbox
from src.skills import load_skills, build_skill_prompt
from src.state.types import ParentState, _merge_ledger, _last
from src.utils.files import load_json, summarize, DATA_DIR

logger = logging.getLogger(__name__)

_FIELD_REDUCERS = {
    "task_ledger": _merge_ledger,
    "messages": lambda a, b: (a or []) + (b or []),
    "dev_events": lambda a, b: (a or []) + (b or []),
}

MAX_RETRY_ROUNDS = 3
REVIEWER_PASS_SCORE = 59

# 所有支持代码生成的动态角色（Developer 管道）
CODE_ROLES = {"frontend", "backend", "data_analysis"}
# Writer 管道覆盖的角色
WRITE_ROLES = {"report"}


# ═══════════════════════════════════════════════════════════════════
# Verifier — 纯代码验证（不消耗 LLM token）
# ═══════════════════════════════════════════════════════════════════

def _verify_code(code: str, role: str) -> str:
    import re as _re, subprocess, tempfile, os as _os
    if not code or len(code) < 100:
        return "⚠️ 代码为空或过短，无法验证"
    results = []

    if role == "frontend":
        files = {}
        file_pat = _re.compile(r'^=== (.+?) ===\n', _re.MULTILINE)
        file_matches = list(file_pat.finditer(code))
        for i, m in enumerate(file_matches):
            fn = m.group(1).strip()
            s = m.end()
            e = file_matches[i+1].start() if i+1 < len(file_matches) else len(code)
            files[fn] = code[s:e].strip()
        html_file = None
        for fn in files:
            if fn.endswith('.html'):
                html_file = fn
                break
        if html_file:
            html = files[html_file]
            results.append(f"✅ HTML: {html_file} ({len(html)}字符)")
            for check, label in [
                ('<table', '表格'), ('<form', '表单'), ('<nav', '导航'),
                ('<button', '按钮'), ('<input', '输入框'), ('<canvas', '图表'),
            ]:
                if check in html.lower():
                    results.append(f"  ✅ 含{label}")
            cdns = _re.findall(r'(?:href|src)=[\"\'](https?://[^\"\']+)[\"\']', html)
            if cdns:
                results.append(f"  ✅ {len(cdns)}个CDN资源")
        js_files = [f for f in files if f.endswith('.js')]
        if js_files:
            total_js = sum(len(files[f]) for f in js_files)
            results.append(f"✅ JS: {len(js_files)}个文件 ({total_js}字符)")
        css_files = [f for f in files if f.endswith('.css')]
        if css_files:
            total_css = sum(len(files[f]) for f in css_files)
            results.append(f"✅ CSS: {len(css_files)}个文件 ({total_css}字符)")
        if not html_file:
            results.append("❌ 未找到HTML文件")

    elif role == "backend":
        files = {}
        file_pat = _re.compile(r'^=== (.+?) ===\n', _re.MULTILINE)
        file_matches = list(file_pat.finditer(code))
        for i, m in enumerate(file_matches):
            fn = m.group(1).strip()
            s = m.end()
            e = file_matches[i+1].start() if i+1 < len(file_matches) else len(code)
            files[fn] = code[s:e].strip()
        for key_file in ['server.js', 'package.json', 'server.py']:
            if key_file in files:
                results.append(f"✅ {key_file}: {len(files[key_file])}字符")
        route_files = [f for f in files if 'route' in f.lower()]
        if route_files:
            results.append(f"✅ 路由: {len(route_files)}个")
        model_files = [f for f in files if 'model' in f.lower()]
        if model_files:
            results.append(f"✅ 模型: {len(model_files)}个")
        if 'server.js' in files:
            try:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8') as f:
                    f.write(files['server.js'])
                    tmp = f.name
                r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=10)
                _os.unlink(tmp)
                if r.returncode == 0:
                    results.append("✅ Node语法检查: 通过")
                else:
                    results.append(f"❌ Node语法错误: {r.stderr.strip()[:200]}")
            except FileNotFoundError:
                results.append("⚠️ Node未安装，跳过语法检查")
            except Exception as e:
                results.append(f"⚠️ 语法检查失败: {str(e)[:100]}")
        if not results or all('❌' not in r for r in results if '⚠️' not in r):
            results.append("✅ 后端文件结构完整")

    else:
        files = {}
        file_pat = _re.compile(r'^=== (.+?) ===\n', _re.MULTILINE)
        file_matches = list(file_pat.finditer(code))
        if file_matches:
            for i, m in enumerate(file_matches):
                fn = m.group(1).strip()
                s = m.end()
                e = file_matches[i+1].start() if i+1 < len(file_matches) else len(code)
                files[fn] = code[s:e].strip()
            results.append(f"✅ [{role}] {len(files)} 个文件: {', '.join(list(files.keys())[:5])}")
            total_chars = sum(len(c) for c in files.values())
            results.append(f"✅ [{role}] 总产出: {total_chars} 字符")
        else:
            results.append(f"✅ [{role}] 产出: {len(code)} 字符")

    return "\n".join(results)


def _cleanup_wal(db_path: str) -> None:
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            try: os.remove(p)
            except OSError: pass
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS writes (thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT, task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))")
        conn.commit(); conn.close()
    except Exception: pass

# ═══════════════════════════════════════════════════════════════════
# 角色 Prompt
# ═══════════════════════════════════════════════════════════════════

ROLE_PROMPTS = {
    "frontend": """你是资深前端UI设计师+开发者。产出必须**美观专业、功能完整**。

## 强制要求（index.html 必须包含）
- Font Awesome 6.4 CDN
- Chart.js 4.4 CDN

## 设计标准（CSS必须遵守）
- 配色：丰富的CSS变量（10+颜色），深蓝紫/青蓝/琥珀金配色
- 卡片：border-radius:12px+、box-shadow多层、hover上浮4px+加深阴影
- 表格：斑马纹、圆角8px、hover高亮行、sticky表头、响应式横向滚动
- 表单：48px高输入框、focus发光ring、错误红色边框
- 按钮：渐变背景、hover上浮、active按下、圆角8px
- 统计卡片：大号数字+Font Awesome图标+渐变背景
- 导航：sticky顶部、backdrop-filter:blur(8px)
- 动效：transition 0.2-0.3s、hover微缩放
- 间距：padding(24px+)、gap(16px+)
- 字体：Inter系统字体、清晰的层级
- 响应式：@media (max-width:768px) 移动端适配

## 技术规则
- 多文件拆分：HTML骨架 + CSS(>=2个) + JS(>=5个模块)
- 每个文件<=300行，禁止单文件
- 使用CSS变量管理配色""",

    "backend": "你是后端开发者。只负责 API、数据库、服务器端逻辑。产出可直接运行的代码文件。",

    "data_analysis": """你是数据分析师。产出完整的数据分析脚本。
要求：
- 读取数据文件（CSV/Excel/JSON）
- 数据探查（列名、类型、统计摘要）
- 分析逻辑（SQL查询或Pandas操作）
- 可视化代码（matplotlib/echarts）
- 分析结论和建议""",

    "report": """你是报告撰写专家。产出结构化 Markdown 报告。
要求：
- 清晰的层级结构（h2/h3/h4）
- 关键数据用表格展示
- 结论和建议突出显示
- 专业的语气和术语""",
}

WRITER_ROLE_PROMPTS = {
    "report": ROLE_PROMPTS["report"],
    "general": "你是技术文档撰写者。产出清晰的技术文档。结构完整（概述/详情/API/示例）。",
}


# ═══════════════════════════════════════════════════════════════════
# 信息封锁适配器
# ═══════════════════════════════════════════════════════════════════

def _to_ceo(state: ParentState) -> dict:
    return {"user_input": state.get("user_input", "")}

def _from_ceo(ceo_output: dict, state: ParentState) -> dict:
    tasks = ceo_output.get("dispatch_tasks", {})
    task_type = ceo_output.get("task_type", "code")
    ledger = []
    for role, t in tasks.items():
        t["status"] = "pending"
        ledger.append(t)
    return {"dispatch_tasks": tasks, "task_ledger": ledger, "task_type": task_type}


# ── Developer 适配器 ──

def _make_to_dev(role: str):
    def _to_dev(state: ParentState) -> dict:
        task = (state.get("dispatch_task") or {})
        research_notes = ""
        retry_feedback = ""
        prev_code = ""

        # 设计调研路径（仅 frontend 使用）
        ctx = state.get("dispatch_context", "") or ""
        if ctx and os.path.exists(str(ctx)) and role == "frontend":
            try:
                data = load_json(ctx)
                research_notes = data.get("final_answer", data.get("answer", ""))
                print(f"     📖 [{role}] 加载设计调研: {len(research_notes)} 字符")
            except Exception:
                research_notes = str(ctx)

        # Reviewer 退回路径
        role_output = state.get("outputs", {}).get(role, {})
        feedback = role_output.get("reviewer_feedback", "") or state.get(f"reviewer_feedback_{role}", "") or state.get("reviewer_feedback", "")
        kept = role_output.get("kept_content", "") or state.get(f"kept_code_{role}", "") or state.get("kept_code", "")
        notebook = state.get(f"notebook_{role}") or state.get("notebook") or {}
        if feedback and kept:
            fix_list = "\n".join(
                f"- {f}: {'; '.join(e.get('issues', [])[:2])}"
                for f, e in notebook.items()
            ) if notebook else "（根据 Reviewer 反馈确定）"
            retry_feedback = f"""## Reviewer 反馈（只修以下问题，保留其余内容）
{feedback}

## 问题文件（只修这些）
{fix_list}

## 上次内容
{kept[:8000]}
"""
            prev_code = kept

        # Role prompt + skill 注入
        role_prompt = ROLE_PROMPTS.get(role, ROLE_PROMPTS.get("frontend", ""))
        try:
            skills = load_skills()
            sp = build_skill_prompt(skills)
            if sp:
                role_prompt = sp + "\n\n" + role_prompt
        except Exception:
            pass

        return {
            "task_description": task.get("description", ""),
            "role_prompt": role_prompt,
            "research_notes": research_notes,
            "retry_feedback": retry_feedback,
            "draft_code": prev_code,
            "certainty_annotations": [],
            "final_code": "",
            "self_check_passed": False,
            "self_check_topics": "",
            "output_path": "",
            "_notebook": notebook,
        }
    return _to_dev


def _make_from_dev(role: str):
    def _from_dev(dev_state: dict, parent_state: ParentState) -> dict:
        path = dev_state.get("output_path", "")
        html_url = dev_state.get("html_url", "")
        task = dict((parent_state.get("dispatch_task") or {}))
        task["result_ref"] = path
        task["summary"] = summarize(path) if path else ""
        task["status"] = "done"
        dev_events = dev_state.get("_events", [])

        role_output = {
            "content": dev_state.get("final_code", ""),
            "content_type": "code",
            "output_path": path,
            "html_url": html_url,
        }

        return {
            "dispatch_task": task, "task_ledger": [task],
            "code": dev_state.get("final_code", ""),
            f"code_{role}": dev_state.get("final_code", ""),
            "output_path": path,
            f"output_path_{role}": path,
            "html_url": html_url,
            f"html_url_{role}": html_url,
            "dev_events": dev_events,
            "outputs": {role: role_output},
        }
    return _from_dev


# ── Writer 适配器 ──

def _make_to_writer(role: str):
    def _to_writer(state: ParentState) -> dict:
        task = (state.get("dispatch_task") or {})
        research_notes = ""
        ctx = state.get("dispatch_context", "") or ""
        if ctx and os.path.exists(str(ctx)):
            try:
                data = load_json(ctx)
                research_notes = data.get("final_answer", data.get("answer", ""))
            except Exception:
                pass
        role_prompt = WRITER_ROLE_PROMPTS.get(role, WRITER_ROLE_PROMPTS["general"])
        return {
            "task_description": task.get("description", ""),
            "role_prompt": role_prompt,
            "research_notes": research_notes,
            "retry_feedback": "",
            "draft_code": "",
            "certainty_annotations": [],
            "final_code": "",
            "self_check_passed": True,
            "self_check_topics": "",
            "output_path": "",
            "_notebook": {},
        }
    return _to_writer


def _make_from_writer(role: str):
    def _from_writer(writer_state: dict, parent_state: ParentState) -> dict:
        path = writer_state.get("output_path", "")
        task = dict((parent_state.get("dispatch_task") or {}))
        task["result_ref"] = path
        task["summary"] = summarize(path) if path else ""
        task["status"] = "done"
        role_output = {
            "content": writer_state.get("final_code", ""),
            "content_type": "report",
            "output_path": path,
            "html_url": "",
        }
        return {
            "dispatch_task": task, "task_ledger": [task],
            "output_path": path, "html_url": "",
            "outputs": {role: role_output},
        }
    return _from_writer


# ── Researcher 适配器 ──

def _make_to_res(role: str = "frontend"):
    def _to_res(state: ParentState) -> dict:
        task = state.get("dispatch_task") or {}
        task_desc = task.get("description", "") or state.get("user_input", "")
        if role == "frontend":
            question = f"""你是前端设计研究员。为以下前端项目调研最佳实践和设计参考。

## 项目需求
{task_desc}

## 调研要求
1. CSS 框架与设计系统推荐
2. UI 布局模式
3. 配色方案（含CSS变量）
4. 组件设计要点
5. 交互细节（hover/动画/响应式）

输出紧凑设计简报。"""
        else:
            question = f"""你是通用知识研究员。为以下问题进行深入调研。

## 调研问题
{task_desc}

## 调研要求
1. 多角度搜索相关信息
2. 提供具体事实、数据、示例
3. 标注信息来源
4. 产出结构化调研报告（Markdown格式）"""
        return {
            "question": question,
            "role_prompt": f"你是{'前端设计' if role == 'frontend' else '通用知识'}研究员。",
            "draft_notes": "", "final_answer": "", "confidence": "HIGH", "output_path": "",
        }
    return _to_res


def _from_res(res_state: dict, parent_state: ParentState) -> dict:
    path = res_state.get("output_path", "")
    return {
        "dispatch_context": path,
        "task_ledger": [{"id": "R", "description": "调研完成",
                          "assignee": "researcher", "role": "research", "status": "done"}],
        "outputs": {"research": {"content": res_state.get("final_answer", ""),
                                  "content_type": "research", "output_path": path}},
    }


# ═══════════════════════════════════════════════════════════════════
# 路由函数
# ═══════════════════════════════════════════════════════════════════

def _route_after_ceo(state: ParentState) -> str:
    """CEO 后路由：根据 task_type 分流."""
    task_type = state.get("task_type", "code")
    tasks = state.get("dispatch_tasks", {})

    if task_type == "code" and tasks.get("frontend"):
        print(f"     🎨 代码任务+前端 -> Researcher 先做设计调研")
        return "researcher_frontend"
    if task_type == "research":
        print(f"     🧠 调研任务 -> Researcher(通用)")
        return "researcher_general"
    return "fanout"


def _fanout(state: ParentState) -> list[Send] | str:
    """动态扇出：遍历 dispatch_tasks 的所有角色."""
    tasks = state.get("dispatch_tasks", {})
    sends = []
    for role, t in tasks.items():
        if not t or not isinstance(t, dict):
            continue
        if t.get("status") != "pending":
            continue
        t = dict(t)
        t["research_count"] = 0
        if t.get("assignee") == "skill_runner":
            sends.append(Send("skill_runner", {"dispatch_task": t}))
        elif role in CODE_ROLES:
            sends.append(Send(f"developer_{role}", {"dispatch_task": t}))
        elif role in WRITE_ROLES:
            sends.append(Send(f"writer_{role}", {"dispatch_task": t}))
        elif role == "research":
            sends.append(Send("researcher_general", {"dispatch_task": t}))
        else:
            sends.append(Send("developer_frontend", {"dispatch_task": t}))

    if not sends:
        sends.append(Send("developer_frontend", {
            "dispatch_task": {"id": "T001", "description": state.get("user_input", ""),
                              "role": "frontend", "status": "pending", "research_count": 0}
        }))
    return sends


def _route_after_dev(state: ParentState) -> str:
    return "reviewer"

def _route_after_writer(state: ParentState) -> str:
    return "reviewer"

def _route_after_research(state: ParentState) -> str:
    task_type = state.get("task_type", "code")
    if task_type == "research":
        return END
    return "fanout"


# ═══════════════════════════════════════════════════════════════════
# Reviewer 节点工厂
# ═══════════════════════════════════════════════════════════════════

def _make_reviewer_node(role: str, content_type: str = "code"):
    def _reviewer_node(state: ParentState) -> dict:
        llm = get_llm()
        role_output = state.get("outputs", {}).get(role, {})
        content = role_output.get("content", "") or state.get(f"code_{role}", "") or state.get("code", "")
        path = role_output.get("output_path", "") or state.get(f"output_path_{role}", "") or state.get("output_path", "")
        task = dict(state.get("dispatch_task") or {})
        task_desc = task.get("description", "")
        retry_count = role_output.get("retry_count", 0) or state.get(f"retry_count_{role}", 0)

        if content_type == "code":
            verify_result = _verify_code(content, role)
            if verify_result:
                print(f"     🔬 [{role}] Verifier: {verify_result[:100]}")
        else:
            verify_result = f"✅ [{role}] 文档/报告类型，跳过代码语法验证"

        # ── 沙箱执行：实际运行代码，拿到真实运行结果 ──
        sandbox_result = run_in_sandbox(content, content_type)
        if not sandbox_result.get("skipped"):
            status = "✅" if sandbox_result.get("passed") else "❌"
            sandbox_type = sandbox_result.get("sandbox_type", "?")
            print(f"     📦 [{role}] Sandbox({sandbox_type}): {status} ({sandbox_result.get('duration_ms', 0)}ms)")
            if sandbox_result.get("error"):
                print(f"        {sandbox_result['error'][:120]}")

        result = reviewer_execute({
            "task_description": task_desc,
            "code": content,
            "output_path": path,
            "verify_result": verify_result,
            "sandbox_result": sandbox_result,
        }, llm, role=role if content_type == "code" else "report")
        review = result.get("review_report", {})
        score = review.get("score", 80)

        if score < REVIEWER_PASS_SCORE and retry_count < MAX_RETRY_ROUNDS:
            retry_count += 1
            issues = review.get("issues", [])
            feedback = "; ".join(issues[:5]) if issues else review.get("summary", "")
            node_name = f"developer_{role}" if content_type == "code" else f"writer_{role}"
            print(f"  📝 [{role}] Reviewer {score}分 < {REVIEWER_PASS_SCORE}，退回 {node_name} (retry={retry_count})")
            return {
                f"review_report_{role}": review,
                "review_report": review,
                f"kept_code_{role}": content,
                f"reviewer_feedback_{role}": feedback,
                f"retry_count_{role}": retry_count,
                "outputs": {role: {"review_report": review, "kept_content": content,
                                    "reviewer_feedback": feedback, "retry_count": retry_count,
                                    "content_type": content_type}},
                "task_ledger": [{"id": f"REVIEW_{role}", "description": review.get("summary", ""),
                                  "assignee": "reviewer", "role": role, "status": "retry"}],
            }

        return {
            f"review_report_{role}": review,
            "review_report": review,
            f"approved_{role}": True,
            "approved": True,
            "outputs": {role: {"review_report": review, "approved": True, "content_type": content_type}},
            "task_ledger": [{"id": f"REVIEW_{role}", "description": review.get("summary", ""),
                              "assignee": "reviewer", "role": role, "status": "done"}],
        }
    return _reviewer_node


def _make_reviewer_route(r: str, content_type: str = "code"):
    def _route(state: ParentState) -> str:
        role_output = state.get("outputs", {}).get(r, {})
        review = role_output.get("review_report") or state.get(f"review_report_{r}") or state.get("review_report") or {}
        score = review.get("score", 80)
        retry_count = role_output.get("retry_count", 0) or state.get(f"retry_count_{r}", 0)
        if score < REVIEWER_PASS_SCORE and retry_count < MAX_RETRY_ROUNDS:
            node_name = f"developer_{r}" if content_type == "code" else f"writer_{r}"
            return node_name
        return END
    return _route


# ═══════════════════════════════════════════════════════════════════
# 图构建
# ═══════════════════════════════════════════════════════════════════

def build_graph(llm=None, checkpointer=None):
    llm = llm or get_llm()
    g = StateGraph(ParentState)

    # ── CEO ──
    def _ceo_node(state: ParentState) -> dict:
        return _from_ceo(analyze_and_dispatch(_to_ceo(state), llm), state)
    g.add_node("ceo", _ceo_node)

    # ── Researcher 子图 (前端设计调研) ──
    res_subgraph = build_researcher_subgraph(llm)
    def _researcher_frontend_node(state: ParentState) -> dict:
        return _from_res(res_subgraph.invoke(_make_to_res("frontend")(state)), state)
    g.add_node("researcher_frontend", _researcher_frontend_node)

    # ── Researcher 子图 (通用调研) ──
    def _researcher_general_node(state: ParentState) -> dict:
        return _from_res(res_subgraph.invoke(_make_to_res("general")(state)), state)
    g.add_node("researcher_general", _researcher_general_node)

    # ── Developer 子图（代码角色）──
    dev_subgraph = build_developer_subgraph(llm)
    for role in CODE_ROLES:
        _to_d = _make_to_dev(role)
        _from_d = _make_from_dev(role)
        def _make_dev_node(_td, _fd, _sg):
            def _dev_node(state: ParentState) -> dict:
                dev_state = _td(state)
                result = _sg.invoke(dev_state)
                return _fd(result, state)
            return _dev_node
        g.add_node(f"developer_{role}", _make_dev_node(_to_d, _from_d, dev_subgraph))

    # ── Writer 子图（文档角色）──
    writer_subgraph = build_writer_subgraph(llm)
    for role in WRITE_ROLES:
        _to_w = _make_to_writer(role)
        _from_w = _make_from_writer(role)
        def _make_writer_node(_tw, _fw, _sg):
            def _writer_node(state: ParentState) -> dict:
                writer_state = _tw(state)
                result = _sg.invoke(writer_state)
                return _fw(result, state)
            return _writer_node
        g.add_node(f"writer_{role}", _make_writer_node(_to_w, _from_w, writer_subgraph))

    # ── SkillRunner 子图（技能脚本执行）──
    skill_subgraph = build_skill_runner_subgraph(llm)
    def _skill_runner_node(state: ParentState) -> dict:
        task = (state.get("dispatch_task") or {})
        skill_name = task.get("role", "")
        user_input = task.get("description", "")
        runner_state = {
            "task_description": skill_name,
            "role_prompt": user_input,
            "research_notes": state.get("dispatch_context", ""),
            "retry_feedback": "", "draft_code": "",
            "certainty_annotations": [], "final_code": "",
            "self_check_passed": True, "self_check_topics": "",
            "output_path": "", "_notebook": {},
        }
        result = skill_subgraph.invoke(runner_state)
        task["result_ref"] = result.get("output_path", "")
        task["summary"] = "技能执行完成"
        task["status"] = "done"
        return {
            "dispatch_task": task,
            "task_ledger": [task],
            "output_path": result.get("output_path", ""),
            "outputs": {"skill": {"content": result.get("final_code", ""),
                                   "content_type": "skill_output", "output_path": result.get("output_path", "")}},
        }
    g.add_node("skill_runner", _skill_runner_node)

    # ── Reviewer 节点 ──
    for role in CODE_ROLES:
        g.add_node(f"reviewer_{role}", _make_reviewer_node(role, "code"))
    for role in WRITE_ROLES:
        g.add_node(f"reviewer_{role}", _make_reviewer_node(role, "report"))

    # ── 连边 ──
    g.set_entry_point("ceo")
    g.add_conditional_edges("ceo", _route_after_ceo, {
        "researcher_frontend": "researcher_frontend",
        "researcher_general": "researcher_general",
        "fanout": "fanout",
    })

    g.add_conditional_edges("researcher_frontend", _route_after_research, {"fanout": "fanout", END: END})
    g.add_conditional_edges("researcher_general", _route_after_research, {"fanout": "fanout", END: END})

    g.add_node("fanout", lambda s: {})
    g.add_conditional_edges("fanout", _fanout)

    # Developer -> Reviewer (代码管道)
    for role in CODE_ROLES:
        g.add_conditional_edges(f"developer_{role}", _route_after_dev, {"reviewer": f"reviewer_{role}"})
        g.add_conditional_edges(f"reviewer_{role}", _make_reviewer_route(role, "code"), {
            f"developer_{role}": f"developer_{role}", END: END,
        })

    # Writer -> Reviewer (文档管道)
    for role in WRITE_ROLES:
        g.add_conditional_edges(f"writer_{role}", _route_after_writer, {"reviewer": f"reviewer_{role}"})
        g.add_conditional_edges(f"reviewer_{role}", _make_reviewer_route(role, "report"), {
            f"writer_{role}": f"writer_{role}", END: END,
        })

    # SkillRunner -> END (脚本执行完直接结束)
    g.add_edge("skill_runner", END)

    return g.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════════════════════════

def _make_initial(user_input: str, thread_id: str) -> dict:
    return {
        "user_input": user_input, "thread_id": thread_id,
        "dispatch_tasks": {}, "dispatch_task": None, "dispatch_context": None,
        "code": "", "output_path": "", "html_url": "",
        "review_report": None, "approved": False,
        "kept_code": "", "reviewer_feedback": "", "research_question": "",
        "task_ledger": [], "messages": [],
        "outputs": {}, "task_type": "code",
    }

def run(user_input: str, thread_id: str | None = None, db_path: str | None = None) -> dict:
    llm = get_llm()
    tid = thread_id or f"session-{time.strftime('%Y%m%d-%H%M%S')}"
    initial = _make_initial(user_input, tid)
    os.makedirs(DATA_DIR, exist_ok=True)
    db = db_path or os.path.join(DATA_DIR, "checkpoints.db")
    _cleanup_wal(db)
    conn = sqlite3.connect(db, check_same_thread=False)
    cp = SqliteSaver(conn=conn); cp.setup()
    graph = build_graph(llm=llm, checkpointer=cp)
    try:
        return graph.invoke(initial, {"configurable": {"thread_id": tid}})
    except Exception as e:
        logger.error("run() error for %s: %s\n%s", tid, e, _tb.format_exc())
        raise
    finally:
        conn.close()

async def run_streaming(user_input: str, thread_id: str | None = None,
                         db_path: str | None = None,
                         event_queue: "asyncio.Queue | None" = None) -> dict:
    llm = get_llm()
    tid = thread_id or f"session-{time.strftime('%Y%m%d-%H%M%S')}"
    initial = _make_initial(user_input, tid)
    os.makedirs(DATA_DIR, exist_ok=True)
    db = db_path or os.path.join(DATA_DIR, "checkpoints.db")
    _cleanup_wal(db)

    async def _push(event: dict):
        if event_queue is not None: await event_queue.put(event)

    await _push({"type": "node_start", "node": "ceo", "data": "CEO 开始分析任务..."})
    last_node = None
    final_state = dict(initial)

    try:
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            graph = build_graph(llm=llm, checkpointer=cp)
            async for chunk in graph.astream(initial, {"configurable": {"thread_id": tid}},
                                              stream_mode="updates"):
                if not chunk or not isinstance(chunk, dict): continue
                for node_name, node_output in chunk.items():
                    if not node_name or not isinstance(node_output, dict): continue
                    if node_name != last_node:
                        last_node = node_name
                        await _push({"type": "node_start", "node": str(node_name),
                                     "data": _node_label(str(node_name))})
                    for ev in _extract_events(str(node_name), node_output):
                        await _push(ev)
                    for k, v in node_output.items():
                        if k is None: continue
                        if k in final_state:
                            reducer = _FIELD_REDUCERS.get(k)
                            if reducer: final_state[k] = reducer(final_state[k], v)
                            elif v is not None: final_state[k] = v
                        else: final_state[k] = v

        ledger = final_state.get("task_ledger") or []
        done = sum(1 for t in ledger if isinstance(t, dict) and t.get("status") == "done")
        review = final_state.get("review_report") or {}
        outputs = final_state.get("outputs") or {}
        research_output = outputs.get("research", {})
        direct = ""
        if research_output.get("content"):
            direct = research_output["content"][:2000]
        elif review.get("summary"):
            score = review.get("score", 0)
            summary = review.get("summary", "完成")
            highlights = review.get("highlights", [])
            hl_text = "\n".join(f"  ✨ {h}" for h in highlights[:3]) if highlights else ""
            direct = f"评分: {score}分\n{summary}\n{hl_text}"
            # 附加产出路径
            op = final_state.get("output_path", "")
            hu = final_state.get("html_url", "")
            if hu:
                direct += f"\n\n🌐 预览: {hu}"
            if op:
                direct += f"\n📁 文件: {op}"

        await _push({"type": "workflow_complete", "thread_id": tid,
                      "task_ledger": ledger, "done": done, "total": len(ledger),
                      "approved": final_state.get("approved", True),
                      "direct_answer": direct,
                      "review_score": review.get("score", 0),
                      "output_path": final_state.get("output_path", ""),
                      "html_url": final_state.get("html_url", "")})
        return final_state
    except Exception as e:
        detail = _tb.format_exc()
        logger.error("run_streaming() error for %s: %s\n%s", tid, e, detail)
        await _push({"type": "error", "data": str(e)})
        raise


def _node_label(n: str) -> str:
    emoji_map = {"ceo": "🎯 CEO", "researcher_frontend": "🧠 Researcher(Design)",
                 "researcher_general": "🧠 Researcher", "fanout": "🔄 Fanout"}
    if n.startswith("reviewer_"):
        role = n.split("_", 1)[1]
        role_emoji = {"frontend": "🎨", "backend": "⚙️", "data_analysis": "📊", "report": "📝"}
        return f"{role_emoji.get(role, '🔍')} Review({role})"
    if n.startswith("developer_"):
        role = n.split("_", 1)[1]
        role_emoji = {"frontend": "🎨", "backend": "⚙️", "data_analysis": "📊"}
        return f"{role_emoji.get(role, '💻')} Dev({role})"
    if n.startswith("writer_"):
        role = n.split("_", 1)[1]
        return f"📝 Writer({role})"
    return emoji_map.get(n, f"▶ {n}")

def _extract_events(node_name: str, output: dict) -> list[dict]:
    if not isinstance(output, dict): return []
    events = []
    task = output.get("dispatch_task") or {}
    tid = task.get("id", "")
    desc = str(task.get("description", ""))[:60]

    if node_name == "ceo":
        ledger = output.get("task_ledger") or []
        for t in ledger:
            if isinstance(t, dict):
                events.append({"type": "task_created", "task_id": t.get("id", ""),
                                "assignee": t.get("assignee", "developer"),
                                "role": t.get("role", "general"),
                                "description": str(t.get("description", ""))[:80]})
    elif node_name.startswith("developer_"):
        role = node_name.split("_", 1)[1]
        events.append({"type": "task_running", "task_id": tid, "task_desc": desc,
                        "node": "developer", "role": role})
        dev_events = output.get("dev_events", [])
        for ev in dev_events:
            if ev.get("type") == "file_plan":
                events.append({"type": "file_plan", "task_id": tid,
                                "files": ev.get("files", []), "total": ev.get("total", 0), "role": role})
            elif ev.get("type") == "file_done":
                events.append({"type": "file_done", "task_id": tid,
                                "file": ev.get("file", ""), "chars": ev.get("chars", 0), "role": role})
            elif ev.get("type") == "file_save":
                events.append({"type": "file_save", "task_id": tid,
                                "total_chars": ev.get("total_chars", 0),
                                "file_count": ev.get("file_count", 0),
                                "html_url": ev.get("html_url", ""), "role": role})
        if output.get("output_path"):
            events.append({"type": "task_done", "task_id": tid, "node": node_name,
                            "role": role, "data": f"代码产出: {output['output_path']}"})
    elif node_name.startswith("writer_"):
        role = node_name.split("_", 1)[1]
        events.append({"type": "task_running", "task_id": tid, "task_desc": desc,
                        "node": "writer", "role": role})
        dev_events = output.get("dev_events", [])
        for ev in dev_events:
            if ev.get("type") == "file_plan":
                events.append({"type": "file_plan", "task_id": tid,
                                "files": ev.get("files", []), "total": ev.get("total", 0), "role": role})
            elif ev.get("type") == "file_done":
                events.append({"type": "file_done", "task_id": tid,
                                "file": ev.get("file", ""), "chars": ev.get("chars", 0), "role": role})
            elif ev.get("type") == "file_save":
                events.append({"type": "file_save", "task_id": tid,
                                "total_chars": ev.get("total_chars", 0),
                                "file_count": ev.get("file_count", 0),
                                "html_url": "", "role": role})
        if output.get("output_path"):
            events.append({"type": "task_done", "task_id": tid, "node": node_name,
                            "role": role, "data": f"报告产出: {output['output_path']}"})
    elif node_name in ("researcher_frontend", "researcher_general"):
        events.append({"type": "task_running", "task_id": f"R-{tid}",
                        "task_desc": "调研中...", "node": "researcher"})
        if output.get("dispatch_context"):
            events.append({"type": "task_done", "task_id": f"R-{tid}",
                            "node": "researcher", "data": "调研完成"})
    elif node_name.startswith("reviewer"):
        role = node_name.split("_", 1)[1] if "_" in node_name else ""
        report = output.get(f"review_report_{role}") or output.get("review_report")
        if isinstance(report, dict):
            events.append({"type": "review", "task_id": f"REVIEW_{role}",
                            "score": report.get("score", 0), "passed": report.get("passed", True),
                            "needs": "done", "summary": str(report.get("summary", ""))[:100]})
    return events

def continue_conversation(thread_id: str, user_input: str, db_path: str | None = None) -> dict:
    return run(user_input, thread_id=thread_id, db_path=db_path)

def list_sessions(db_path: str | None = None) -> list[dict]:
    path = db_path or os.path.join(DATA_DIR, "checkpoints.db")
    if not os.path.exists(path): return []
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT thread_id, MAX(checkpoint_id) as steps FROM checkpoints GROUP BY thread_id ORDER BY steps DESC").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError: return []
    finally: conn.close()
