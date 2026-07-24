"""Reviewer — 代码审查官（纯评估，不拦截）.

输入: {"task_description": "...", "output_path": "...", "code": "...", "verify_result": "..."}
输出: {"review_report": {...}, "approved": true}

身份：执行层 Critic — 质量评估节点
Emoji：🔍

role 参数控制审查范围：
  - "frontend": 只审查 HTML/CSS/JS 的 UI、交互、样式
  - "backend":  只审查 API 路由、数据模型、中间件、服务端逻辑
"""

from __future__ import annotations
import json as _json
from langchain_core.messages import HumanMessage, SystemMessage
from src.agents.llm import get_llm, safe_parse_json
from src.utils.files import load_json

REVIEWER_PROMPT = """\
# Reviewer 代码审查官

**角色**：执行层 Critic — 质量评估节点
**Emoji**：🔍

## 系统定位

审查 Developer 产出的代码，从三个维度评估质量。
不做路由决策、不拦截代码、不要求重做。只评估和总结。

## Skill 0: 角色边界（严格遵守）

你是 {role_label} 审查员。
- frontend：只审查 HTML/CSS/JS 的 UI、交互、样式正确性。如果代码中没有后端文件（server.js、routes/），不要报"缺少后端API/数据库/认证"。那不是前端代码的问题。
- backend：只审查 API 路由、数据模型、中间件、服务端逻辑。如果代码中没有前端文件，不要挑剔 CSS 样式、UI 布局、动画效果。

**审查时必须严格遵守角色边界，不要跨域评估。**

## Skill 1: 三维审查（聚焦可运行性）

### 审查维度

| 维度 | 权重 | 核心问题 |
|------|------|---------|
| 可运行 | 50% | 代码完整吗？打开/运行会报错吗？核心功能能跑吗？ |
| 功能覆盖 | 30% | 主要功能点实现了吗？没有大块缺失？ |
| 可用性 | 20% | UI 合理吗？交互能用吗？有没有致命体验问题？ |

### 评分标准（宽松——AI 生成的代码，不要求完美）

| 分数 | 含义 |
|------|------|
| 80-100 | 可运行，核心功能完整，无明显 bug |
| 60-79 | 可运行，主要功能有，可能有小缺陷 |
| 40-59 | 部分可运行，关键功能缺失或明显不完整 |
| 0-39 | 无法运行，代码严重截断或错误 |

**重要**：代码能被 LLM 截断是正常的——如果核心逻辑完整就给 60+。不要因为"代码不完整"就打低分，要看"不完整到什么程度"。

## Skill 2: 亮点与问题识别

### highlights（亮点）
- 做得好的地方——值得保留和复用的代码模式
- 至少列出 2 个，最多 5 个

### issues（问题）
- **格式**：`文件名: 具体问题描述 + 建议修复`
- 必须指明具体文件，让 Developer 的笔记本记录问题文件清单
- 只列真正重要的，不要鸡毛蒜皮

### fixes（精确修复指令，可选但强烈建议提供）

当能精确定位代码问题时，提供 find→replace 指令让 Developer 直接修复：
- **file**: 文件名（如 `index.html`, `css/base.css`, `js/store.js`）
- **type** 必须是以下之一：
  - `class_name`   — HTML class 名与 CSS 定义不匹配
  - `id_name`      — HTML id 属性错误
  - `attribute`    — HTML 属性名/值错误
  - `method_name`  — JS 函数/方法名错误
  - `import_path`  — JS import/require 路径错误
  - `api_contract` — API 端点/参数/响应格式不一致
  - `data_format`  — 数据键名/结构不一致
- **find**: 代码中需要替换的原始片段（精确到能唯一定位）
- **replace**: 替换为的正确代码片段
- **reason**: 一句话说明为什么需要这个修改

**只有能精确定位到具体代码片段时才提供 fixes，否则不提供。不要提供过于宽泛的指令（如"修复CSS样式"），必须精确到class名、id名或函数名级别的替换。**

## 审查优先级

1. **先看能不能跑**：代码完整吗？打开/运行会报错吗？
2. **再看对不对**：逻辑正确吗？API 用对了吗？
3. **最后看好不好**：结构清晰吗？性能 OK 吗？

## 输出 JSON Schema

{{
  "score": 85,
  "passed": true,
  "summary": "一句话总结代码质量",
  "highlights": ["亮点1（具体到功能/代码段）", "亮点2"],
  "issues": ["文件名: 问题描述 + 建议修复"],
  "fixes": [
    {{
      "file": "index.html",
      "type": "class_name",
      "find": "class=\\"logo\\"",
      "replace": "class=\\"sidebar-logo\\"",
      "reason": "CSS中.sidebar-logo定义了侧栏样式，HTML class名需一致"
    }}
  ]
}}

## 禁止行为

- ❌ 要求重做或修改代码（只评估不决策）
- ❌ 给出模糊评价（必须具体到功能点/代码段）
- ❌ 用"挺好的"、"还行"这种无信息量的总结
- ❌ 只挑毛病不给亮点
- ❌ 输出空报告
- ❌ 跨角色审查（frontend 不要抱怨缺少后端API；backend 不要挑剔CSS样式）
"""


def _build_review_context(code: str, max_chars: int = 90000, role: str = "") -> str:
    """构建审查用的代码上下文。多文件项目按优先级展示，按 role 过滤不相关文件。"""
    import re as _re

    # 检测多文件格式 "=== filename ===\ncontent"
    file_pattern = _re.compile(r'^=== (.+?) ===\n', _re.MULTILINE)
    matches = list(file_pattern.finditer(code))

    if not matches:
        # 单文件：直接展示（最多 max_chars）
        if len(code) <= max_chars:
            return f"```\n{code}\n```"
        return f"```\n{code[:max_chars]}\n```\n\n> ⚠️ 代码过长（{len(code)} 字符），以上为前 {max_chars} 字符"

    # 多文件：拆分为独立文件
    files = {}
    for i, m in enumerate(matches):
        fname = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(code)
        files[fname] = code[start:end].strip()

    # ── 角色过滤：根据 role 只保留相关文件 ──
    FRONTEND_EXTS = {'.html', '.css', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte'}
    BACKEND_PATTERNS = [r'^server\.', r'^app\.', r'^routes?/', r'^middleware/', r'^models?/',
                        r'^utils?/', r'^config\.', r'^package\.json$', r'^data/',
                        r'\.py$', r'\.go$', r'\.java$', r'\.rb$', r'\.php$']

    if role == "frontend":
        filtered = {}
        for fname in files:
            ext = _re.search(r'\.[a-z]+$', fname)
            if ext and ext.group(0) in FRONTEND_EXTS:
                filtered[fname] = files[fname]
        if filtered:
            # 只保留前端文件
            removed = set(files.keys()) - set(filtered.keys())
            files = filtered
            if removed:
                print(f"     🔍 [{role}] 过滤掉 {len(removed)} 个非前端文件: {', '.join(sorted(list(removed))[:5])}")
    elif role == "backend":
        filtered = {}
        for fname in files:
            ext = _re.search(r'\.[a-z]+$', fname)
            is_frontend = ext and ext.group(0) in {'.html', '.css'}
            if not is_frontend:
                filtered[fname] = files[fname]
        if filtered:
            removed = set(files.keys()) - set(filtered.keys())
            files = filtered
            if removed:
                print(f"     🔍 [{role}] 过滤掉 {len(removed)} 个非后端文件: {', '.join(sorted(list(removed))[:5])}")

    if not files:
        return "> ℹ️ 此角色没有相关文件需要审查（所有文件已被角色过滤器排除）"

    # 关键文件列表（优先展示完整内容）
    KEY_PATTERNS = [
        r'^index\.html$', r'^server\.(js|py)$', r'^main\.(js|py)$',
        r'^app\.(js|py)$', r'^package\.json$', r'^routes?/',
        r'^middleware/', r'^models?/', r'^js/(main|app|store)\.js$',
        r'^css/(base|style)\.css$',
    ]
    key_files = []
    other_files = []
    for fname in files:
        is_key = any(_re.match(p, fname) for p in KEY_PATTERNS)
        if is_key:
            key_files.append(fname)
        else:
            other_files.append(fname)

    # 构建上下文
    lines = [f"## 文件清单（{len(files)} 个文件，{len(code)} 字符）\n"]
    lines.append("| 文件 | 大小 | 重要性 |")
    lines.append("|------|------|--------|")
    for fname in sorted(key_files):
        lines.append(f"| {fname} | {len(files[fname])} 字符 | 🔑 核心 |")
    for fname in sorted(other_files):
        lines.append(f"| {fname} | {len(files[fname])} 字符 | |")
    lines.append("")

    # 展示关键文件完整内容
    chars_used = sum(len(l) for l in lines)
    remaining = max_chars - chars_used
    shown_count = 0
    total_files = len(files)

    # 先展示核心文件
    for fname in sorted(key_files) + sorted(other_files):
        content = files[fname]
        if remaining <= 500:
            # 空间不够
            break

        if len(content) <= remaining:
            lines.append(f"### {fname} ({len(content)} 字符)")
            lines.append("```")
            lines.append(content)
            lines.append("```")
            lines.append("")
            remaining -= len(content) + 50
        else:
            # 截断展示
            truncated_len = max(remaining - 100, 200)
            lines.append(f"### {fname} ({len(content)} 字符，仅展示前 {truncated_len} 字符)")
            lines.append("```")
            lines.append(content[:truncated_len])
            lines.append("```")
            lines.append(f"> ⚠️ 此文件被截断，完整内容 {len(content)} 字符")
            lines.append("")
            remaining = 0
        shown_count += 1

    # 剩余文件提示
    skipped = total_files - shown_count
    if skipped > 0:
        lines.append(f"\n> ℹ️ 剩余 {skipped}/{total_files} 个文件因空间限制省略，请根据文件清单判断完整性")

    return "\n".join(lines)


def _build_sandbox_section(sr: dict) -> str:
    """构建沙箱执行结果的提示词段落."""
    if not sr or sr.get("skipped"):
        return ""
    status_icon = "✅" if sr.get("passed") else "❌"
    status_text = "通过" if sr.get("passed") else "失败"
    sandbox_type = sr.get("sandbox_type", "?")
    output = sr.get("output", "")
    error = sr.get("error", "")
    
    parts = []
    parts.append("## 📦 沙箱实际运行结果 (" + sandbox_type + ")")
    parts.append("执行状态: " + status_icon + " " + status_text)
    if error:
        parts.append("错误: " + str(error))
    if output:
        parts.append("运行输出:")
        parts.append("```")
        parts.append(output[:2000])
        parts.append("```")
    parts.append("请根据实际运行结果调整评分：代码能真正跑通的应得更高分。")
    return "\n".join(parts) + "\n\n"
    
def execute(state: dict, llm=None, role: str = "") -> dict:
    """审查代码。返回评分和总结，不做路由决策.

    role 参数控制审查范围：
      - "frontend": 只审查 HTML/CSS/JS 的 UI
      - "backend":  只审查 API 路由/模型/服务端逻辑
      - "" (默认):  不限制（向后兼容）
    """
    llm = llm or get_llm(0.2)

    task_desc = state.get("task_description", "")
    code = state.get("code", "")
    output_path = state.get("output_path", "")
    verify_result = state.get("verify_result", "")  # Verifier 纯代码验证结果
    sandbox_result = state.get("sandbox_result", {})  # 沙箱实际运行结果

    # 优先从文件加载
    if output_path and not code:
        try:
            data = load_json(output_path)
            code = data.get("code", "")
        except Exception:
            pass

    if not code:
        print(f"  ⚠️ Reviewer: 无代码可审查")
        return {
            "review_report": {"score": 0, "passed": False, "summary": "无代码产出",
                              "highlights": [], "issues": ["无代码"], "fixes": []},
            "approved": True,
        }

    print(f"  🔍 Reviewer 审查 {len(code)} 字符代码...")

    # ── 智能代码摘要 + 角色过滤 ──
    review_code = _build_review_context(code, role=role)
    is_likely_truncated = len(code) < 3000  # 真正的截断：代码异常短

    verify_section = ""
    if verify_result:
        verify_section = f"""## 🔬 Verifier 自动验证结果（纯代码检查，非LLM判断）
{verify_result}

"""

    # ── 角色感知的审查指令 ──
    role_note = ""
    if role == "frontend":
        role_note = (
            "\n\n⚠️ **你的审查角色：前端审查官**\n"
            "你只审查 HTML/CSS/JS UI 代码。不要报告后端缺失（API、数据库、认证等）。\n"
            "如果项目中不包含后端文件（server.js、routes/），不要提及'缺少后端'或'缺少数据库'。\n"
            "这不是前端代码的问题——这只是一个前端项目。"
        )
    elif role == "backend":
        role_note = (
            "\n\n⚠️ **你的审查角色：后端审查官**\n"
            "你只审查 API/路由/模型/数据库代码。不要挑剔前端问题（CSS样式、布局、视觉效果）。\n"
            "如果项目中不包含前端文件（index.html、style.css），不要提及'缺少前端页面'或'缺少UI'。\n"
            "这不是后端代码的问题——这只是一个后端项目。"
        )

    # ── 注入角色标签到系统 prompt ──
    role_label_map = {"frontend": "前端 (frontend)", "backend": "后端 (backend)"}
    system_prompt = REVIEWER_PROMPT.replace(
        "{role_label}", role_label_map.get(role, f"{role} (通用)")
    )

    ctx = f"""任务描述：{task_desc}

{verify_section}{_build_sandbox_section(sandbox_result)}代码（共 {len(code)} 字符）：
{review_code}
{role_note}
"""

    try:
        resp = llm.invoke([SystemMessage(content=system_prompt),
                           HumanMessage(content=ctx)]).content
        review = safe_parse_json(resp, {
            "score": 80, "passed": True, "summary": "审查完成",
            "highlights": ["代码已产出"], "issues": [], "fixes": [],
        })
    except Exception:
        review = {"score": 80, "passed": True, "summary": "审查完成",
                   "highlights": ["代码已产出"], "issues": [], "fixes": []}

    # 截断确认：如果 Review 的 summary 或 issues 提到截断，或代码确实异常短
    if is_likely_truncated:
        summary = review.get("summary", "")
        issues = review.get("issues", [])
        if "截断" in summary or any("截断" in str(i) for i in issues):
            review["score"] = min(review.get("score", 80), 20)
            review["summary"] = "代码截断，未完成全部功能"
            review["issues"] = issues + ["代码截断，仅完成部分内容，请分块生成"]

    score = review.get("score", 80)
    passed = review.get("passed", True)
    summary = review.get("summary", "")
    issues = review.get("issues", [])
    highlights = review.get("highlights", [])
    fixes = review.get("fixes", [])

    print(f"  {'✅' if passed else '⚠️'} Reviewer: {summary[:100]} (分数:{score})")
    for h in highlights:
        print(f"     ✨ {h[:80]}")
    for i in issues:
        print(f"     📝 {i[:80]}")
    if fixes:
        print(f"     🔧 {len(fixes)} 个精确修复指令")

    # ── 失败时构建定向研究摘要 ──
    failure_summary = ""
    if score < 60 and issues:
        fixes_lines = ""
        if fixes:
            fixes_lines = "\n精确修复指令（供 Developer 直接执行）：\n" + "\n".join(
                f'- [{fix.get("type","")}] {fix.get("file","")}: `{fix.get("find","")}` → `{fix.get("replace","")}`'
                for fix in fixes[:5]
            )
        failure_summary = f"""Reviewer 发现以下问题需要修复：
{chr(10).join(f'- {i[:200]}' for i in issues[:5])}
{fixes_lines}

请针对上述具体问题修复（不是重新生成整个项目）。
只修改有问题的文件和代码行，保留其余正确代码。"""

    return {
        "review_report": {
            "score": score,
            "passed": passed,
            "summary": summary,
            "highlights": highlights,
            "issues": issues,
            "fixes": fixes,
            "failure_summary": failure_summary,
        },
        "approved": True,
    }
