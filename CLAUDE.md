# Shuchiin Academy — 项目宪法

## 注释管理规则

### bug 标注注释：修完即删

代码中使用以下标注来标记 bug 和隐患，**修复后必须删除或改为"已修复"**：

| 标注 | 含义 | 修复后处理 |
|------|------|-----------|
| `🔴 致命BUG` / `🔴 BUG HERE` | 确定存在的 bug | **删除整段注释** |
| `🟡 注意` / `🟡 隐患` | 潜在风险或设计缺陷 | **删除或改为 `✅ 已修复: ...`** |
| `# 原代码: ...` / `# 应该改为:` | 修复前后对比 | **删除**（代码本身已是正确答案） |

### 禁止行为

- ❌ 修完 bug 后保留 `🔴 BUG HERE` 注释（会让人以为 bug 还在）
- ❌ 保留 "原代码 / 应该改为" 的 diff 式注释（过时信息污染阅读体验）
- ❌ 保留 "此函数从未被接入" 等已解决的接入问题注释

### 正确示例

```python
# ❌ 修复后残留
def _from_dev(dev_state, parent_state):
    """🔴 致命BUG: parent_() 未定义！原代码: task = parent_(...)"""
    task = dict(parent_state.get("dispatch_task") or {})  # 代码已修，注释还在
    ...

# ✅ 修复后干净
def _from_dev(dev_state, parent_state):
    """Developer 子图 → 父图状态映射。"""
    task = dict(parent_state.get("dispatch_task") or {})
    ...
```

### 例外

- 未修复的已知 bug 保留标注（但不应该长期存在）
- 设计层面的取舍说明（需注明原因，而非简单标记）

---

## GStack 工作流（强制）

**每次执行任务必须遵循原生 GStack 并行 Sprint 模式**。

参考：[Garry Tan's gstack](https://github.com/garrytan/gstack) | [架构文档](https://github.com/aelhadi/ai.gstack/blob/main/ARCHITECTURE.md)

### 核心概念

将 Claude Code 从单一助手转变为**虚拟工程团队**（~23 个专业 Agent + 8 个工具），遵循固定 Sprint 管线：

```
Think → Plan → Build → Review → Test → Ship → Reflect
```

### 并行 Sprint 模型

每个任务启动独立的并行 Sprint，在隔离的 git worktree 中运行：

| Sprint | 角色 | 职责 |
|--------|------|------|
| Sprint A | Planner | 需求分析、任务拆解、输出实施计划 |
| Sprint B | Reviewer | 代码审查，独立于实施 |
| Sprint C | Implementer | 按计划写代码 |
| Sprint D | QA | 在真实浏览器/终端中验证 |

### 执行规则

1. **并行优先**：独立子任务必须用 Agent 工具并行启动，禁止串行等待
2. **隔离环境**：互相冲突的任务用 `isolation: "worktree"` 隔离开
3. **固定管线**：每个 Sprint 内部顺序执行 Think→Plan→Build→Review→Test→Ship
4. **先计划后执行**：任何代码改动前，先用 Plan agent 设计方案
5. **独立验证**：实施和审查必须是不同的 Agent，禁止自写自查

### 禁止行为
- ❌ 不分析直接改代码
- ❌ 可并行的任务串行执行
- ❌ 同一 Agent 既写代码又审查自己的代码
- ❌ 修改后不验证就跑
