# Shuchiin Academy — 四 Agent 并行工作流

基于 LangGraph 的 **CEO → Researcher(前置调研) → Developer×2(并行) → Reviewer×2(独立审查)** 管道。

## 当前架构（2026-07-22）

```
🎯 CEO (拆解任务)
     │
     ├─ [有前端任务] → 🧠 Researcher (前端设计调研: 配色/布局/组件)
     │                        │
     │                        └→ fanout
     │
     └─ fanout (Send 并行扇出)
           │
           ├─ 🎨 Dev(frontend) ──→ 🔍 Reviewer(frontend)
           │                            │
           │                  score<59 → Dev (直接回退，增量修复)
           │                  score≥59 → END
           │
           └─ ⚙️ Dev(backend)  ──→ 🔍 Reviewer(backend)
                                        │
                              score<59 → Dev (直接回退，增量修复)
                              score≥59 → END
```

### 四 Agent 职责

| Agent | Emoji | 输入 | 输出 |
|-------|-------|------|------|
| **CEO** | 🎯 | user_input | dispatch_tasks{frontend, backend} |
| **Researcher** | 🧠 | 前端任务描述 | 设计简报（配色/布局/组件要点/参考链接）→ dispatch_context |
| **Developer** | 🔧 | task_description + role_prompt + research_notes + kept_code + reviewer_feedback | code + output_path + html_url |
| **Reviewer** | 🔍 | task_description + code + verify_result | review_report + 若退回: kept_code + reviewer_feedback |

### 关键参数

| 参数 | 值 | 含义 |
|------|-----|------|
| `REVIEWER_PASS_SCORE` | 59 | 低于 59 分退回重做 |
| `MAX_RETRY_ROUNDS` | 3 | 最多退回重做 3 次 |
| `max_tokens` | 16384 | LLM 输出上限（StepFun/DeepSeek） |

### Researcher 前置调研（仅前端项目）

CEO 分析出有前端任务后，先调用 Researcher 做设计调研（配色方案 CSS 变量、布局模式、组件要点、参考链接），产出 `dispatch_context` 文件路径。Developer 启动时读取调研结果，获得设计方向指引。

### 增量修复机制（Reviewer 退回时）

```
Reviewer 退回时不丢弃代码：
  kept_code       = 上次完整代码（好的部分保留）
  reviewer_feedback = 具体问题列表（Developer 只看这个）
  notebook        = 问题文件映射 {filename: {issues, fix_status}}

Developer 收到：
  draft_code = kept_code（在此基础改，不从头写）
  retry_feedback = reviewer_feedback（只修这些问题）
```

### Verifier — 纯代码验证

Reviewer 调用前，先通过 `_verify_code()` 做纯代码验证（不消耗 LLM token）：
- 前端：检查 HTML 结构、CSS 变量、JS 模块、CDN 引用
- 后端：检查 server.js/package.json 存在、Node 语法检查
- 验证结果传给 Reviewer 作为评分参考

---

## 快速开始

```bash
cd ~/"Shuchiin Academy"
cp config.yaml.example config.yaml  # 填入 API Key
pip install -e .

# Web 服务 (FastAPI + WebSocket 实时进度)
python src/server.py
# 打开 http://localhost:8000

# CLI 模式
python -m src "做一个打飞机游戏"
```

---

## 项目结构

```
Shuchiin Academy/
├── config.yaml              # API Key + 模型配置
├── pyproject.toml
├── README.md
├── skills/                   # Skill 目录 (22 个 public skill)
│   ├── public/               # 内置 skill (从 deer-flow 移植)
│   ├── custom/               # 用户自定义 skill
│   └── legacy/               # 旧版 skill
├── static/index.html        # Web UI
├── src/
│   ├── main.py              # CLI 入口
│   ├── server.py            # FastAPI + WebSocket
│   ├── config.py            # 配置加载 (YAML + 环境变量)
│   ├── agents/
│   │   ├── ceo.py           # CEO: 任务拆解 → dispatch_tasks
│   │   ├── developer.py     # Developer: plan → write_parallel → save → END
│   │   ├── researcher.py    # Researcher: 前端设计调研（前置，非管道内调用）
│   │   ├── reviewer.py      # Reviewer: 三维审查（可运行50%+功能30%+可用性20%）
│   │   └── llm.py           # LLM 工厂 + JSON 安全解析
│   ├── graph/
│   │   └── orchestrator.py  # LangGraph 图构建 + 适配器 + 流式运行
│   ├── skills/              # Skill 系统 (四层: types/parser/loader/prompt)
│   │   ├── types.py         # Skill 数据类
│   │   ├── parser.py        # SKILL.md YAML 解析
│   │   ├── loader.py        # 目录扫描加载
│   │   └── prompt.py        # XML 提示词注入
│   ├── state/
│   │   └── types.py         # ParentState, DeveloperState, ResearcherState
│   └── utils/
│       └── files.py         # JSON 文件读写 (Agent 间通信媒介)
└── data/
    ├── checkpoints.db       # LangGraph SQLite 状态持久化
    ├── outputs/             # Developer 代码产物 (JSON)
    ├── research/            # Researcher 调研产物 (JSON)
    └── pages/               # HTML 页面直出 (可直接浏览器打开)
```

---

## 动态任务路由（v2.1）

### 四种任务类型

CEO 自动识别用户输入，分派到对应管道：

| 任务类型 | 触发词 | 管道 |
|---------|--------|------|
| **code** | 游戏/网站/API/管理系统 | Researcher → Developer → Reviewer → END |
| **research** | 调研/研究/搜索 | Researcher(通用) → END |
| **analysis** | 数据/统计/图表 | Developer → Reviewer → END |
| **report** | 报告/文档/PPT | Writer → Reviewer → END |
| **skill** | `/image-generation` 等 | SkillRunner → END |

### 图节点（14个）

```
ceo → researcher_frontend / researcher_general / fanout
fanout → developer_frontend / developer_backend / developer_data_analysis
         / writer_report / skill_runner
developer_* → reviewer_* → END
writer_report → reviewer_report → END
skill_runner → END
```

---

## Skill 系统（四层架构 + 执行层）

### 架构

```
skills/                          ← Layer 1: 文件约定
├── public/                      ← 内置 skill，只读
│   ├── deep-research/SKILL.md   ← 每个 skill 一个目录 + SKILL.md
│   └── ... (22 个 skill)
└── custom/                      ← 用户自定义 skill

src/skills/
├── types.py    ← Skill 数据类 (含 executor/runtime/entrypoint)
├── parser.py   ← YAML frontmatter 解析
├── loader.py   ← os.walk 扫描 + 自动检测可执行脚本
└── prompt.py   ← Skill 索引 → XML 提示词注入

src/agents/skill_runner.py  ← Skill 脚本执行器
```

### 自动检测可执行技能

Loader 自动扫描每个 skill 目录，发现有 `scripts/` 目录且未声明 `executor` 时，自动设置 `executor=script` 并推断 runtime：

| 后缀 | runtime |
|------|---------|
| `.py` | `python` |
| `.js` | `node` |
| `.sh` | `bash` |

当前自动检测到 **13 个可执行技能**（image-generation, ppt-generation, chart-visualization 等）。

### `/skill-name` 快速路由

用户输入 `/image-generation 生成熊猫` → CEO 直接创建 skill 任务，跳过 LLM 分类：

```
/_ → SkillRunner:
  1. 读 SKILL.md Markdown 正文（完整执行指令）
  2. LLM 理解意图 → 生成脚本 CLI 参数
  3. subprocess 执行 scripts/generate.py
  4. 保存结果 → END
```

### Skill 的 SKILL.md 格式

```markdown
---
name: deep-research
description: Use this skill when researching any topic online...
executor: prompt           # "prompt" | "script"
runtime: ""                # 仅 executor=script: python | node | bash
entrypoint: ""             # 仅 executor=script: scripts/generate.py
---

# Deep Research Skill

## Overview
...
```

---

## 沙箱执行系统

```
Reviewer 节点内置三层验证:
  1. _verify_code()      ← 静态代码结构验证（不消耗 LLM token）
  2. run_in_sandbox()    ← 沙箱实际运行（Docker 容器 或 subprocess 降级）
  3. reviewer_execute()  ← LLM 审查（综合前两步结果评分）

src/sandbox/
├── __init__.py     ← 导出 run_in_sandbox
└── executor.py     ← 沙箱执行引擎
```

### content_type 执行策略

| content_type | 执行方式 | 说明 |
|-------------|---------|------|
| code_node | Docker `npm test` / subprocess `node --check` | Node.js 代码 |
| code_python | Docker `pytest` / subprocess `py_compile` | Python 代码 |
| code_html | 静态 HTML/CSS/JS 结构验证 | 不消耗运行时 |
| report/research | 跳过 | 不需要运行时验证 |

### Docker 降级

```
Docker daemon 可用？ → 容器内运行 (512MB/断网/64进程限制)
    ↓ 不可用
subprocess → node --check / python -m py_compile (仅语法检查)
```

---

## 用户宪法文件系统

用户偏好存储在 `~/.shuchiin/constitution.yaml`，每次 CEO 启动时自动注入 prompt。

```yaml
# ~/.shuchiin/constitution.yaml
preferences:
  tech_stack:
    frontend: "React + TypeScript"
    backend: "FastAPI"
  output:
    explain_after: false  # 直接干活不解释
current_project:
  name: "Shuchiin Academy"
```

空宪法 → 返回空字符串 → 不占用 token。

API: `GET/POST /api/constitution`

### 设计决策：为什么不用自动记忆提取

| | 宪法文件 | 自动记忆提取 |
|---|---|---|
| Token 开销 | ~200（精准） | ~800（含噪声） |
| 正确率 | 100%（用户写的） | ~70%（LLM 猜的） |
| 维护成本 | 手动更新 | 自动 |
| 适合场景 | 单用户/少用户 | 大规模多用户 |

当前选择宪法文件优先，自动提取留待以后按需加入。

---

## 工具系统（Tool Registry）

### 设计原则

200个工具全放磁盘 ≠ 200个工具全塞prompt。Prompt 里只放 3-5 个活跃的。

```
tools/registry.py (磁盘，不进prompt)
  ┌──────────────────┐
  │ 200个工具定义     │
  │ web_search       │
  │ github_search    │        tool_search("数据库")
  │ brave_search     │  ────────────────────────→  LLM prompt (只3个)
  │ filesystem_read  │                              postgres_query
  │ ...              │                              mysql_query
  └──────────────────┘                              redis_get
```

### 当前 5 个内置工具

| 工具 | 说明 |
|------|------|
| `web_search` | DuckDuckGo 搜索，免费无 API Key |
| `read_page` | 读取网页纯文本内容 |
| `github_search_repos` | GitHub API 搜索仓库 |
| `filesystem_read` | 读取本地文件 |
| `filesystem_list` | 列出目录内容 |

### Prompt 占用

| 工具数 | Prompt 字符 |
|--------|-----------|
| 1 个 | ~200 chars |
| 5 个 | ~500 chars |
| 200 个（全放） | ~20000 chars |

### 工具调用循环（Researcher 通用调研模式）

```
LLM 输出: {"tool_search": "react"} 
  → Python: registry.search("react")
  → 返回: [web_search, github_search_repos]
  → 注入 prompt

LLM 输出: {"tool_call": "web_search", "arguments": {"query": "React 19"}}
  → Python: execute_tool("web_search", {"query": "React 19"})
  → 返回: 真实搜索结果
  → 反馈给 LLM → LLM 基于真实数据写报告
```

最多 5 轮工具调用，之后强制 LLM 给出最终答案。

### 添加新工具

```python
# src/tools/registry.py → BUILTIN_TOOLS 列表
ToolDef(
    name="postgres_query",
    description="执行 SQL 查询",
    tags=["database", "sql", "postgres"],
    parameters={...},
    endpoint="builtin",
)
# + src/tools/executor.py → 添加执行逻辑
```

---

## 已解决的核心问题

### 🔴 输出截断（2026-07-20）

**症状**：LLM 每次输出 ~2000 字就截断，复杂任务代码永远写不完整。

**根因**：`get_llm()` 没传 `max_tokens`，StepFun 模型默认 4096 tokens ≈ 2000 中文字。

**修复**：`llm.py` 默认 `max_tokens=16384`，输出容量 ×4。config.yaml 新增 `max_tokens` 配置项。

### 🔴 重做=推倒重来（2026-07-20）

**症状**：Reviewer 退回重做时，Developer 不知道哪里好哪里坏，全部重写，结果可能更差。第一次 45 分 → 第二次 45 分 → 第三次 30 分。

**根因**：退回时只传了 issue 文本，没有保留好代码，Developer 不知道要保留什么。

**修复**：增量修复机制——Reviewer 退回时保留 `kept_code`（好代码），Developer 在此基础上只修问题部分。流程改为 Reviewer → Developer(增量修) → Reviewer(再审)。

### 🔴 前端 dump 源码（2026-07-20）

**症状**：Web UI 的 Answer 框显示 5 万字符完整源码 JSON。

**修复**：`direct_answer` 改为 Reviewer 摘要，前端只显示预览链接 + 评分 + 任务列表。

### 🟡 BrokenPipeError 崩溃（2026-07-16）

**症状**：Developer 的 `print()` 在 uvicorn reload 时 stdout 管道断开，整个工作流崩溃。

**修复**：`server.py` 启动时用 `_SafeStdout` 包装 `sys.stdout/stderr`，BrokenPipeError 静默忽略。

### 🟡 uvicorn reload 杀工作流（2026-07-16）

**症状**：每次改代码 uvicorn reload 重启 worker，正在跑的工作流状态损坏。

**修复**：SQLite checkpoint 持久化 + `_cleanup_wal()` 清理损坏的 WAL 文件。

### 🟡 StepFun API 频繁超时（2026-07-16）

**症状**：日志中 22 次 `Retrying request`，4 次 `APITimeoutError`。

**根因**：StepFun API 对长响应不稳定 + httpx 底层 read timeout 不受 `config.yaml` 控制。

**修复**：`max_tokens=16384` 让单次调用产生更多代码，减少调用次数。httpx 超时由 langchain-openai 的 `timeout` 参数统一控制。

---

## 架构演进（关键节点）

| 日期 | 变更 |
|------|------|
| 07-14 | v1: CEO→Developer→Reviewer→ceo_feedback→Researcher 动态路由 |
| 07-15 | v2: Send 扇出 4 角色并行 + dispatch_tasks 字典 + Command 路由 |
| 07-16 | v3: 放弃动态工作流，简化为 CEO→Researcher→Developer→Reviewer 线性管道 |
| 07-16 | v4: 并行 Developer + [NEEDS_RESEARCH] 标记 + 信息封锁 |
| 07-20 | v5: 增量修复（kept_code+reviewer_feedback）+ Reviewer→Researcher→Developer 回传 + max_tokens 16K |
| 07-22 | v6: 简化——Researcher 从管道内回退改为前置设计调研（仅前端）；Reviewer 直接回退 Developer，不经过 Researcher；新增 Verifier 纯代码验证 |

---

## Skill 系统（四层架构）

### 架构

```
skills/                          ← Layer 1: 文件约定
├── public/                      ← 内置 skill，只读
│   ├── deep-research/SKILL.md   ← 每个 skill 一个目录 + SKILL.md
│   ├── data-analysis/SKILL.md   ← YAML frontmatter (name, description)
│   └── ... (22 个 skill)
└── custom/                      ← 用户自定义 skill，可编辑

src/skills/
├── types.py    ← Layer 1: Skill 数据类
├── parser.py   ← Layer 2: SKILL.md → Skill 对象 (YAML frontmatter 解析)
├── loader.py   ← Layer 3: os.walk 扫描加载，按 name 去重排序
└── prompt.py   ← Layer 4: list[Skill] → XML 提示词注入
```

### Skill 的 SKILL.md 格式

```markdown
---
name: deep-research
description: Use this skill when researching any topic online...
---

# Deep Research Skill

## Overview
...
```

YAML frontmatter 中 `name` 和 `description` 必填。`---` 之后是完整的 Markdown 指令正文。

### 前端 / 补全

在输入框中输入 `/` 自动弹出可用 skill 下拉列表，支持模糊搜索。选择后自动填充 `/skill-name `。

API: `GET /api/skills` → `{"skills": [...], "names": [...]}`

---

## 设计原则

1. **信息封锁**：每个 Agent 通过 input/output 适配器只看到自己需要的字段
2. **文件通信**：Agent 产出写入 JSON，state 只存路径，不传原始内容
3. **并行优先**：独立任务通过 Send 并行执行，互不阻塞
4. **增量修复**：退回重做保留好代码，只修问题部分
5. **3 次 fallback**：retry 退回最多 3 次，超过强制通过
6. **前置调研**：前端任务在编码前先做设计调研（Researcher），避免方向偏离
7. **SQLite 持久化**：LangGraph checkpoint 支持断点恢复
8. **纯代码验证**：Reviewer 调用前先跑 Verifier（不消耗 LLM token），验证结果辅助评分
