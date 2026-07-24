"""Developer — 代码生产者（策略B：多文件拆分 + 并行生成）.

子图: _plan → _write_parallel → _save → END
所有文件在 _write_parallel 中通过 ThreadPoolExecutor 并发生成。
"""

from __future__ import annotations
import os as _os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from src.agents.llm import get_llm, safe_parse_json
from src.state.types import DeveloperState
from src.utils.files import save_json, DATA_DIR

# 策略B默认文件清单（复杂前端项目）
DEFAULT_FILES = [
    "index.html",
    "css/base.css",
    "js/store.js",
    "js/table.js",
    "js/form.js",
    "js/search.js",
    "js/stats.js",
    "js/main.js",
]

# 简单项目默认文件
SIMPLE_FILES = ["index.html", "style.css", "app.js"]

PLAN_PROMPT = """\
你是项目规划师。列出项目需要创建的所有文件清单。

## 强制拆分规则（策略B — 功能模块拆分）

### 核心原则
- **禁止单文件**：任何项目都不能只生成一个 HTML 文件
- 每个文件职责单一，≤ 300 行，避免 LLM 输出截断
- 文件名要具体（如 js/store.js，不是"数据层代码"）

### HTML 单页应用（图书馆管理、后台系统、工具页面等）
必须拆分为以下模块：

- index.html — HTML 骨架，只包含结构 + 引入 CSS/JS 的 <link>/<script>
- css/base.css — CSS 变量、布局、表格样式、表单样式、弹窗样式
- js/store.js — 数据持久化层（localStorage CRUD）
- js/table.js — 表格渲染与交互
- js/form.js — 表单逻辑（添加/编辑/验证）
- js/search.js — 搜索与筛选
- js/stats.js — 统计面板
- js/main.js — 入口文件，初始化与事件绑定

### 简单项目（游戏、计算器、小工具）
至少拆为 3 个文件：index.html + style.css + app.js

### 后端项目
package.json → server.js → routes/*.js → models/*.js

## 输出 JSON
{{
  "files": ["index.html", "css/base.css", "js/store.js", "js/table.js", "js/form.js", "js/search.js", "js/stats.js", "js/main.js"]
}}
"""

WRITE_ONE_PROMPT = """\
{role_prompt}

## 项目整体任务
{task_description}

{research_context}

## 并行生成的所有文件（确保你的产出与这些文件的 API/命名一致）
{already_done}

{retry_context}

{related_context}
## 当前要生成的文件：{current_file}

## 规则
- **只输出这个文件的内容**，不要输出其他文件
- 完整实现，不写 TODO、占位符
- 直接写代码，不需要 markdown 代码块包裹
- **如果是CSS文件**：必须包含丰富的渐变、阴影、圆角、过渡动画、hover效果。配色用CSS变量。每个组件至少3种状态（默认/hover/active）。要让人一眼觉得"好看"
- **如果是HTML文件**：结构清晰，class命名规范，预留CSS变量和JS钩子
- 如果有设计调研（research_context），务必使用其中的配色变量和布局建议
- 如果这是 retry（修复模式）：只修改有问题的部分，保留其余
- **如果提供了关联文件内容（related_context），必须确保你代码中引用的class名、id名、函数名、数据键名与关联文件完全一致**
- **如果提供了精确修复指令，必须按照 find→replace 执行修改**
"""


def _plan(state: DeveloperState, llm=None) -> dict:
    """生成文件清单（策略B多文件拆分）。Retry 模式只规划 Reviewer 指出的问题文件。"""
    llm = llm or get_llm(0.2)
    task = state.get("task_description", "")
    role = state.get("role_prompt", "")
    retry_feedback = state.get("retry_feedback", "")
    draft_code = state.get("draft_code", "")

    # ── Retry 模式：增量修复，只重新生成有问题的文件 ──
    if retry_feedback and draft_code:
        return _plan_retry(state, llm, retry_feedback, draft_code)

    # ── 首次生成：LLM 规划文件清单 ──
    task_lower = task.lower()
    is_complex = any(kw in task_lower for kw in [
        "管理", "系统", "后台", "博客", "电商", "图书", "表格",
        "增删改查", "crud", "搜索", "筛选", "统计", "分页",
        "manage", "admin", "system", "dashboard", "table",
    ])

    try:
        resp = llm.invoke([SystemMessage(content=PLAN_PROMPT),
                           HumanMessage(content=f"项目：{task[:300]}")]).content
        plan = safe_parse_json(resp, {
            "files": DEFAULT_FILES if is_complex else SIMPLE_FILES
        })
    except Exception:
        plan = {"files": DEFAULT_FILES if is_complex else SIMPLE_FILES}

    queue = plan.get("files", DEFAULT_FILES if is_complex else SIMPLE_FILES)

    # 安全检查：永远不返回单文件
    if len(queue) < 2:
        queue = SIMPLE_FILES

    print(f"     📋 文件计划: {queue}")

    return {
        "_file_queue": queue,
        "_current_idx": 0,
        "_generated_files": {},
        "_events": [{"type": "file_plan", "files": queue, "total": len(queue)}],
    }


def _plan_retry(state: DeveloperState, llm, retry_feedback: str, draft_code: str) -> dict:
    """Retry 模式：从笔记本（orchestrator 已构建）直接读取问题文件清单，无需 LLM。"""
    import re as _re

    # 解析 draft_code 中的已有文件
    existing_files = {}
    file_pattern = _re.compile(r'^=== (.+?) ===\n', _re.MULTILINE)
    matches = list(file_pattern.finditer(draft_code))
    for i, m in enumerate(matches):
        fname = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(draft_code)
        existing_files[fname] = draft_code[start:end].strip()

    if not existing_files:
        print(f"     📋 Retry 模式无已有文件，使用标准规划")
        return _plan(state, llm)

    # ── 从 orchestrator 传入的笔记本直接读取，不调 LLM ──
    notebook = state.get("_notebook") or {}

    # 如果笔记本为空（兼容旧路径），回退到 LLM 构建
    if not notebook:
        notebook = _build_notebook(retry_feedback, existing_files, llm)

    fix_files = [f for f in notebook if f in existing_files]

    if not fix_files:
        fix_files = list(existing_files.keys())[:3]
        for f in fix_files:
            notebook[f] = {"issues": ["Reviewer反馈需修复"], "keywords": [], "fix_status": "pending", "exact_fixes": []}

    # 笔记本摘要
    nb_summary = []
    for fname in fix_files:
        entry = notebook.get(fname, {})
        kw_str = ", ".join(entry.get("keywords", [])[:3])
        issue_str = "; ".join(entry.get("issues", [])[:2])
        nb_summary.append(f"  📓 {fname}: {issue_str} | 关键词: {kw_str}")

    print(f"     📓 笔记本: {len(fix_files)} 个问题文件/{len(existing_files)} 总文件")
    for line in nb_summary:
        print(line)

    return {
        "_file_queue": fix_files,
        "_current_idx": 0,
        "_generated_files": dict(existing_files),
        "_notebook": notebook,
        "_events": [{"type": "file_plan", "files": fix_files, "total": len(fix_files)}],
    }


def _build_notebook(retry_feedback: str, existing_files: dict, llm,
                    exact_fixes: list | None = None) -> dict:
    """从 Reviewer 反馈构建笔记本：{filename: {issues, keywords, fix_status, exact_fixes}}."""
    import re as _re

    # 先尝试正则提取 "文件名: 问题描述" 格式的 issues
    notebook = {}
    issue_pattern = _re.compile(r'([\w/.-]+\.[a-z]+)\s*[:：]\s*(.+?)(?=$|\n|(?:\s*[\w/.-]+\.[a-z]+\s*[:：]))', _re.MULTILINE | _re.DOTALL)
    for m in issue_pattern.finditer(retry_feedback):
        fname = m.group(1).strip()
        issue_text = m.group(2).strip()[:120]
        if fname in existing_files:
            if fname not in notebook:
                notebook[fname] = {"issues": [], "keywords": [], "fix_status": "pending", "exact_fixes": []}
            notebook[fname]["issues"].append(issue_text)

    # 如果正则没提取到，用 LLM 分析
    if not notebook:
        file_list = "\n".join(f"- {f}" for f in sorted(existing_files.keys()))
        prompt = f"""你是代码问题分析师。从 Reviewer 反馈中提取问题文件清单。

## Reviewer 反馈
{retry_feedback[:1500]}

## 已有文件
{file_list}

## 输出 JSON
{{{{"problem_files": [{{{{"file": "文件名", "issues": ["问题1"], "keywords": ["关键词1", "关键词2"]}}}}]}}}}
关键词是搜索修复方案时最有用的技术术语（如 "CORS中间件"、"SQL注入参数化"、"JWT验证"）。
"""
        try:
            resp = llm.invoke([SystemMessage(content=prompt),
                               HumanMessage(content="提取问题文件")]).content
            result = safe_parse_json(resp, {"problem_files": []})
            for entry in result.get("problem_files", []):
                fname = entry.get("file", "")
                if fname in existing_files:
                    notebook[fname] = {
                        "issues": entry.get("issues", [])[:3],
                        "keywords": entry.get("keywords", [])[:5],
                        "fix_status": "pending",
                        "exact_fixes": [],
                    }
        except Exception:
            pass

    # 如果还是没有，用前3个文件作为兜底
    if not notebook:
        for f in list(existing_files.keys())[:3]:
            notebook[f] = {"issues": ["Reviewer反馈需修复"], "keywords": [], "fix_status": "pending", "exact_fixes": []}

    # ── 注入结构化修复指令（来自 Reviewer 的 fixes 字段）──
    if exact_fixes:
        for fix in exact_fixes:
            if not isinstance(fix, dict):
                continue
            fname = fix.get("file", "")
            if fname and fname in existing_files:
                if fname not in notebook:
                    notebook[fname] = {"issues": [], "keywords": [], "fix_status": "pending", "exact_fixes": []}
                if "exact_fixes" not in notebook[fname]:
                    notebook[fname]["exact_fixes"] = []
                notebook[fname]["exact_fixes"].append({
                    "type": fix.get("type", ""),
                    "find": fix.get("find", ""),
                    "replace": fix.get("replace", ""),
                    "reason": fix.get("reason", ""),
                })
                # 将 fix type 加入关键词
                fix_type = fix.get("type", "")
                kw_map = {
                    "class_name": "CSS类名匹配", "id_name": "ID匹配",
                    "attribute": "HTML属性", "method_name": "函数方法名",
                    "import_path": "文件引用路径", "api_contract": "API契约",
                    "data_format": "数据格式",
                }
                kw = kw_map.get(fix_type, fix_type)
                if kw and kw not in notebook[fname]["keywords"]:
                    notebook[fname]["keywords"].append(kw)

    # 为每个文件提取关键词（如果 LLM 没给）
    for fname, entry in notebook.items():
        if not entry["keywords"] and entry["issues"]:
            # 简单关键词提取：从 issue 中取技术术语
            keywords = _extract_keywords(" ".join(entry["issues"]))
            entry["keywords"] = keywords

    return notebook


def _extract_keywords(text: str) -> list[str]:
    """从文本中提取技术关键词。"""
    import re as _re
    # 匹配常见技术术语模式
    patterns = [
        r'(?:缺少|缺失|未实现|未定义|错误|语法错误|类型错误|引用错误)[：:]*\s*(\S+)',
        r'(SQL注入|XSS|CSRF|CORS|JWT|OAuth|REST|API|DOM|CSS|HTML)',
        r'(中间件|路由|模型|控制器|验证|认证|授权|加密|哈希)',
        r'(参数化|预处理|转义|过滤|校验)',
        r'(express|koa|react|vue|angular|node|npm|webpack)',
    ]
    keywords = set()
    for p in patterns:
        for m in _re.finditer(p, text, _re.IGNORECASE):
            kw = m.group(1).strip()
            if len(kw) > 1:
                keywords.add(kw)
    return list(keywords)[:5]


def _get_related_files(filename: str, generated_files: dict, max_total_chars: int = 3000) -> dict:
    """返回当前文件引用的关联文件内容 {related_filename: content_snippet}.

    解析引用关系：
    - HTML: <link href> → CSS 文件, <script src> → JS 文件
    - JS: import/require → 其他 JS 模块
    - CSS: @import → 其他 CSS 文件

    用于让 Developer 在修文件时看到关联文件的实际 class 名、id 名、函数签名。"""
    import re as _re

    if not generated_files or filename not in generated_files:
        return {}

    content = generated_files.get(filename, "")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # basename → full key 查找表
    basename_map: dict[str, str] = {}
    for fkey in generated_files:
        bn = fkey.rsplit("/", 1)[-1] if "/" in fkey else fkey
        basename_map[bn] = fkey
        basename_map[fkey] = fkey

    def _resolve_ref(ref_path: str) -> str | None:
        clean = ref_path.strip().strip("'\"")
        if clean.startswith("./"): clean = clean[2:]
        # 去掉 query string
        clean = clean.split("?")[0]
        if clean in basename_map:
            return basename_map[clean]
        bn = clean.rsplit("/", 1)[-1] if "/" in clean else clean
        if bn in basename_map:
            return basename_map[bn]
        return None

    related: dict[str, str] = {}
    seen: set[str] = set()

    # ── HTML: 提取 <link> 和 <script> ──
    if ext == "html":
        for m in _re.finditer(
            r'<link[^>]+rel\s*=\s*["\']stylesheet["\'][^>]+href\s*=\s*["\']([^"\']+)["\']',
            content, _re.IGNORECASE,
        ):
            ref = m.group(1).strip()
            if "://" in ref: continue  # CDN
            resolved = _resolve_ref(ref)
            if resolved and resolved != filename and resolved not in seen:
                seen.add(resolved)
                related[resolved] = generated_files[resolved]

        for m in _re.finditer(
            r'<script[^>]+src\s*=\s*["\']([^"\']+)["\']',
            content, _re.IGNORECASE,
        ):
            ref = m.group(1).strip()
            if "://" in ref: continue  # CDN
            resolved = _resolve_ref(ref)
            if resolved and resolved != filename and resolved not in seen:
                seen.add(resolved)
                related[resolved] = generated_files[resolved]

    # ── JS: 提取 import/require ──
    elif ext == "js":
        for m in _re.finditer(
            r"""(?:import\s+(?:[\s\S]*?\s+from\s+)?["']([^"']+)["']|require\s*\(\s*["']([^"']+)["']\s*\))""",
            content,
        ):
            ref = m.group(1) or m.group(2)
            if not ref or "://" in ref or ref.startswith("node:"):
                continue
            resolved = _resolve_ref(ref)
            if not resolved:
                resolved = _resolve_ref(ref + ".js")
            if resolved and resolved != filename and resolved not in seen:
                seen.add(resolved)
                related[resolved] = generated_files[resolved]

    # ── CSS: 提取 @import ──
    elif ext == "css":
        for m in _re.finditer(
            r"""@import\s+(?:url\(\s*)?["']([^"'\s]+)["']""",
            content, _re.IGNORECASE,
        ):
            ref = m.group(1).strip()
            if "://" in ref: continue
            resolved = _resolve_ref(ref)
            if resolved and resolved != filename and resolved not in seen:
                seen.add(resolved)
                related[resolved] = generated_files[resolved]

    if not related:
        return {}

    # ── 截断到 max_total_chars ──
    capped: dict[str, str] = {}
    total = 0
    per_file_cap = max(800, max_total_chars // max(len(related), 1))
    for fname, fcontent in sorted(related.items()):
        snippet = fcontent[:per_file_cap]
        if total + len(snippet) > max_total_chars:
            remaining = max_total_chars - total
            if remaining > 200:
                snippet = snippet[:remaining]
            else:
                break
        capped[fname] = snippet
        total += len(snippet)

    return capped


def _write_parallel(state: DeveloperState, llm=None) -> dict:
    """并行生成所有文件。每个文件一个独立的 LLM 调用，多线程并发。"""
    llm = llm or get_llm(0.4)

    queue = state.get("_file_queue", SIMPLE_FILES)

    # 空队列保护：避免 ThreadPoolExecutor(max_workers=0) 崩溃
    if not queue:
        print("     ⚠️ 文件队列为空，跳过生成")
        return {"_generated_files": {}, "_current_idx": 0,
                "_events": list(state.get("_events", []))}

    role = state.get("role_prompt", "你是全栈开发者。")
    task = state.get("task_description", "")
    research = state.get("research_notes", "")
    retry_feedback = state.get("retry_feedback", "")
    draft_code = state.get("draft_code", "")

    # 并行 worker 数：环境变量可控，默认 3（两条 lane 并行时 3+3=6，避开 API 8 并发限制）
    max_workers = min(len(queue), int(_os.environ.get("DEV_PARALLEL_WORKERS", "3")))

    research_ctx = ""
    if research:
        research_ctx = f"## 🎨 前端设计调研（必须参考其中的配色、布局、组件代码）\n{research[:6000]}"
    retry_ctx = ""
    if retry_feedback and draft_code:
        retry_ctx = f"""## 修复模式
Reviewer 反馈：{retry_feedback[:1500]}

上次代码参考（保留好的部分）：
{draft_code[:2000]}"""

    siblings = "\n".join(f"- {f}" for f in queue)
    total = len(queue)

    print(f"     🚀 并行生成 {total} 个文件 (workers={max_workers}): {queue}")

    # 保留已有文件（retry 模式下 _generated_files 已预填），只覆盖重新生成的
    generated = dict(state.get("_generated_files", {}))
    # 保留 _plan 阶段的事件，追加新事件
    events: list[dict] = list(state.get("_events", []))
    events.append({"type": "file_plan", "files": list(queue), "total": total})
    lock = threading.Lock()
    completed_count = [0]

    def write_single_file(current_file: str) -> tuple[str, str]:
        """生成单个文件。每个线程创建独立的 LLM 实例。"""
        thread_llm = get_llm(0.4)

        # ── 笔记本：当前文件的问题和修复指引 ──
        notebook = state.get("_notebook") or {}
        nb_entry = notebook.get(current_file, {})
        nb_context = ""
        if nb_entry:
            issues = nb_entry.get("issues", [])
            keywords = nb_entry.get("keywords", [])
            exact_fixes = nb_entry.get("exact_fixes", [])
            parts = []
            if issues:
                parts.append(f"## 📓 笔记本：此文件需要修复的问题\n{chr(10).join(f'- {i}' for i in issues[:3])}")
            if exact_fixes:
                fix_lines = []
                for fix in exact_fixes[:5]:
                    t = fix.get('type', '')
                    f = fix.get('find', '')
                    r = fix.get('replace', '')
                    reason = fix.get('reason', '')
                    fix_lines.append(f"  - [{t}] 将 `{f}` 替换为 `{r}` ({reason})")
                parts.append(f"## 🔧 精确修复指令（必须执行）\n{chr(10).join(fix_lines)}")
            if parts:
                parts.append("修复此文件时，请针对上述问题定向修改，保留其余正确代码。")
                nb_context = "\n".join(parts) + "\n"

        # ── 关联文件上下文 ──
        related_context = ""
        if generated and current_file in generated:
            related = _get_related_files(current_file, generated)
            if related:
                related_parts = []
                for rf_name, rf_content in related.items():
                    related_parts.append(
                        f"### {rf_name}（你的代码必须与此文件定义的类名/ID/API保持一致）\n```\n{rf_content}\n```"
                    )
                related_context = f"""## 📎 关联文件内容
{chr(10).join(related_parts)}

请确保当前文件中的class名、id名、函数名、数据键名与上述关联文件完全一致。
"""

        prompt = WRITE_ONE_PROMPT.format(
            role_prompt=role,
            task_description=task[:500],
            already_done=f"并行生成的兄弟文件（与你同时开发，保持 API/命名一致性）:\n{siblings}",
            research_context=research_ctx,
            retry_context=retry_ctx + nb_context,
            current_file=current_file,
            related_context=related_context,
        )

        try:
            resp = thread_llm.invoke([
                SystemMessage(content=prompt),
                HumanMessage(content=f"生成文件：{current_file}")
            ]).content
        except Exception as e:
            print(f"     ❌ {current_file} 生成失败: {e}")
            resp = f"/* [ERROR: {e}] */"

        code = resp.strip() or ""
        # 去掉可能的 markdown 代码块包裹
        if code.startswith("```"):
            import re as _re
            m = _re.search(r"```(?:\w+)?\s*\n(.*?)```", code, _re.DOTALL)
            if m:
                code = m.group(1).strip()

        with lock:
            completed_count[0] += 1
            print(f"     📄 {current_file}: {len(code)} 字符 ({completed_count[0]}/{total})")

        return (current_file, code)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(write_single_file, f): f for f in queue}
        for future in as_completed(futures):
            fname, content = future.result()
            generated[fname] = content
            with lock:
                events.append({
                    "type": "file_done",
                    "file": fname,
                    "chars": len(content),
                })

    return {
        "_generated_files": generated,
        "_current_idx": total,
        "_events": events,
    }


def _save(state: DeveloperState) -> dict:
    """收集所有文件，组装最终代码和 HTML 预览."""
    generated = state.get("_generated_files", {})
    events = list(state.get("_events", []))

    if not generated:
        return {
            "final_code": "",
            "output_path": "",
            "_events": events,
        }

    # ── 收集 HTML ──
    html_content = ""
    for fname, content in generated.items():
        if fname.endswith('.html') or fname == 'index.html':
            html_content = content
            break

    # ── 收集所有 CSS ──
    css_parts = []
    for fname in sorted(generated.keys()):
        if fname.endswith('.css') or '/css/' in fname.replace('\\', '/'):
            content = generated[fname]
            css_parts.append(f"/* === {fname} === */\n{content}")
    css_content = "\n\n".join(css_parts)

    # ── 收集所有 JS ──
    js_parts = []
    for fname in sorted(generated.keys()):
        if fname.endswith('.js') or '/js/' in fname.replace('\\', '/'):
            content = generated[fname]
            js_parts.append(f"// === {fname} ===\n{content}")
    js_content = "\n\n".join(js_parts)

    # ── 内联到 HTML 生成预览 ──
    if html_content:
        import re as _re
        # 只删除本地 CSS 引用（相对路径），保护 CDN 资源
        html_content = _re.sub(
            r'<link[^>]*rel\s*=\s*["\']stylesheet["\'][^>]*href\s*=\s*["\'](?!(?:https?:)?//)[^"\']*["\'][^>]*/?>',
            '', html_content, flags=_re.IGNORECASE
        )
        if css_content:
            if "</head>" in html_content:
                html_content = html_content.replace(
                    "</head>", f"<style>\n{css_content}\n</style>\n</head>"
                )
            else:
                html_content = f"<style>\n{css_content}\n</style>\n{html_content}"
        # 只删除本地 JS 引用（相对路径），保护 CDN 资源
        html_content = _re.sub(
            r'<script[^>]*src\s*=\s*["\'](?!(?:https?:)?//)[^"\']*["\'][^>]*>\s*</script>',
            '', html_content, flags=_re.IGNORECASE
        )
        if js_content:
            if "</body>" in html_content:
                html_content = html_content.replace(
                    "</body>", f"<script>\n{js_content}\n</script>\n</body>"
                )
            else:
                html_content = f"{html_content}\n<script>\n{js_content}\n</script>"

    # ── 保存预览 HTML ──
    static_dir = _os.path.join(DATA_DIR, "pages")
    _os.makedirs(static_dir, exist_ok=True)
    ts = __import__('datetime').datetime.now().strftime("%Y%m%dT%H%M%S")
    html_file = _os.path.join(static_dir, f"output_{ts}.html")
    if html_content:
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)
    html_path = f"/api/pages/output_{ts}.html" if html_content else ""

    # ── 多文件格式存档 ──
    parts = []
    for fname, content in generated.items():
        parts.append(f"=== {fname} ===\n{content}")
    full_code = "\n\n".join(parts)

    path = save_json("outputs", "dev", "D", {"code": full_code, "files": generated})

    total_chars = sum(len(c) for c in generated.values())
    print(f"     💾 保存: {len(generated)} 文件, {total_chars} 字符, 预览: {html_path or '无'}")

    events.append({
        "type": "file_save",
        "total_chars": total_chars,
        "file_count": len(generated),
        "html_url": html_path,
    })

    return {
        "output_path": path,
        "final_code": full_code,
        "html_url": html_path,
        "_generated_files": generated,
        "_events": events,
    }


def build_developer_subgraph(llm=None) -> StateGraph:
    """构建 Developer 子图：_plan → _write_parallel → _save → END."""
    llm = llm or get_llm()
    g = StateGraph(DeveloperState)
    g.add_node("plan", lambda s: _plan(s, llm))
    g.add_node("write_parallel", lambda s: _write_parallel(s, llm))
    g.add_node("save", _save)
    g.set_entry_point("plan")
    g.add_edge("plan", "write_parallel")
    g.add_edge("write_parallel", "save")
    g.add_edge("save", END)
    return g.compile()
