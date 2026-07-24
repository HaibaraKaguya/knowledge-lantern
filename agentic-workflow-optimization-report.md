# Agentic Workflow Optimization 形象化解读报告

**原文:** From Static Templates to Dynamic Runtime Graphs: A Survey of Workflow Optimization for LLM Agents
**arXiv:** https://arxiv.org/abs/2603.22386
**GitHub:** https://github.com/IBM/awesome-agentic-workflow-optimization
**报告整理日期:** 2026-07-15

---

## 0. 先看你的项目：它到底是什么？

在讲论文之前，我先说清楚：**你的项目 "Shuchiin Academy" 就是一个活的 Agentic Workflow 系统。** 它完美对应了这篇论文讨论的所有概念。

你的系统长这样：

```
用户输入
  │
  ▼
┌─────────┐    Send() 扇出     ┌──────────────┐
│   CEO   │ ───────────────→  │ Developer_*  │ (4个并行)
│ 任务拆解 │ ←──────────────── │ (前端/后端/安全)│
└────┬────┘    feedback路由     └──────┬───────┘
     │                                │
     │                    ┌───────────┴───────────┐
     │                    │      Reviewer        │
     │                    │   (双维度审查官)       │
     │                    └───────────┬───────────┘
     │                                │
     │          ┌─────────────────────┼─────────────────────┐
     │          │                     │                     │
     ▼          ▼                     ▼                     ▼
  END    ceo_feedback           researcher           developer_重做
        (纯代码路由)           (知识调研)             (代码重写)
```

**关键特征：**
- 图结构是**固定的**：ceo → developer → reviewer → ceo_feedback/researcher，节点和边在部署前就定好了
- 但运行时是**动态的**：Send() 创建 4 条并行分支，Command() 根据审查结果路由，retry 机制会触发重做
- 子图隔离：Developer 和 Researcher 的内部状态对父图不可见，只通过 JSON 文件通信

这篇论文就是教你：**什么时候该固定、什么时候该动态、动态到什么程度。**

---

## 1. 核心比喻：骨架 vs 血液

把 Agent 系统想象成一个人：

| 论文概念 | 人体比喻 | 你的项目对应 |
|----------|----------|-------------|
| **ACG Template (Ḡ)** | 骨骼/骨架 | `build_graph()` 里固定的节点和边 |
| **Realized Graph (G_run)** | 当前激活的神经通路 | 运行时 Send() 创建的 4 条并行分支 |
| **Execution Trace (τ)** | 血液/神经信号 | `task_ledger`、JSON 文件、streaming 事件 |
| **Static Optimization** | 先天的反射弧 | 固定的 reviewer 快速通道 (≥90分直接放行) |
| **Dynamic Optimization** | 后天学习的条件反射 | CEO 根据审查结果路由到 researcher 或 developer |

**核心论点：** 骨骼决定了你最多能跑多快，但血液流动的路径决定了你此刻在干什么。**两者都重要，但优化策略完全不同。**

---

## 2. 静态 vs 动态：不是二选一，而是配比问题

论文把工作流分成两大阵营，但你的项目恰恰是**两者的混合体**——这正是难点所在。

### 2.1 静态部分（你的骨架）

你的系统中**不变**的东西：

1. **图结构固定**：`build_graph()` 里注册的节点是固定的
   - 1 个 CEO
   - 4 个 Developer (frontend/backend/auth/general)
   - 1 个 Reviewer
   - 1 个 Researcher
   - 1 个 ceo_feedback

2. **边固定**：
   - ceo → Send() → developer_*
   - developer_* → reviewer（所有角色走同一条边）
   - reviewer → ceo_feedback（静态边）
   - ceo_feedback → END（兜底）
   - researcher → developer_*（通过 `_route_after_research` 条件边）

3. **节点内部流程固定**：
   - Developer: write → self_check → save（线性三步）
   - Researcher: research → save（线性两步）
   - Reviewer: 审查 → 返回 report（单步）

4. **角色 prompt 固定**：
   ```python
   ROLE_PROMPTS = {
       "frontend": "你是前端开发者...",
       "backend": "你是后端开发者...",
       ...
   }
   ```

### 2.2 动态部分（你的血液）

你的系统中**运行时变化**的东西：

1. **Send() 并行扇出**：CEO 拆解任务后，`_fanout()` 根据 `dispatch_tasks` 里有几个 pending 角色，就创建几条并行分支。**这是动态选择——从固定的"超级图"里激活子集。**

2. **Command() 动态路由**：`handle_feedback` 根据 Reviewer 的结果决定去哪：
   - `needs="done"` → 找同角色下一个任务或 END
   - `needs="research"` → 跳转到 researcher
   - `needs="retry"` → 跳转回同角色 developer

3. **dispatch_tasks 字典**：每个角色一个槽位，CEO 填充，运行时动态更新。这是你的系统的"动态调度表"。

4. **熔断机制**：retry ≥ 3 时，自动采纳历史最高分的代码。**这是运行时修改执行策略。**

5. **Reviewer 快速通道**：score ≥ 90 时，Reviewer 自己跳转到下一个 developer，跳过 CEO。**这是执行中编辑的轻量版。**

### 2.3 你的系统在论文分类中的位置

按论文的两个关键描述符：

| 描述符 | 你的系统 | 含义 |
|--------|----------|------|
| **GDT (Graph Determination Time)** | pre-execution + in-execution | 结构在 CEO 分析时确定（pre），但 Command 路由在运行时决定（in-execution） |
| **GPM (Graph Plasticity Mode)** | select + edit | Send() 是 select（从固定超级图激活子图），Command() 是 edit（运行时修改路由） |

**你的系统不是纯静态，也不是纯动态——它是"静态骨架 + 动态血液"的混合体。**

---

## 3. 论文分类体系与你项目的对照

论文提出了静态优化和动态优化的分类，我把它和你的代码逐项对照。

### 3.1 静态优化方法（你的项目做到了哪些）

| 方法 | 论文代表 | 你的项目 | 状态 |
|------|----------|----------|------|
| **离线模板搜索** | AFlow (MCTS搜索) | ❌ 没有自动搜索最优图结构 | 缺失 |
| **固定支架内节点优化** | DSPy (prompt编译) | ⚠️ 部分：你有角色 prompt，但没有自动 prompt 优化 | 基础版 |
| **联合优化** | Maestro (结构+配置交替) | ❌ 没有 | 缺失 |
| **可验证性导向优化** | VFlow (硬件verifier) | ⚠️ 部分：Developer 有自检，Reviewer 有审查，但没有自动搜索 | 基础版 |

**你的系统属于"手工设计的静态模板 + 基础节点优化"。** 图结构是人工设计的，prompt 是硬编码的，没有自动搜索/进化过程。

### 3.2 动态优化方法（你的项目做到了哪些）

| 方法 | 论文代表 | 你的项目 | 状态 |
|------|----------|----------|------|
| **选择与剪枝** | AgentDropout, MasRouter | ✅ Send() 扇出（选择激活哪些分支）<br>✅ Reviewer 快速通道（跳过 CEO）<br>✅ 熔断（retry≥3 采纳最高分） | 已有 |
| **构造后执行** | FlowReasoner, Workflow-R1 | ❌ 没有为每个输入生成专属图 | 缺失 |
| **执行中编辑** | DyFlow, AgentConductor | ⚠️ 轻量版：Command() 路由 + retry 重做，但图结构本身不变 | 基础版 |

**你的系统在动态方面做的是"选择/剪枝"（从固定超级图里选子图激活），但没有"生成新图"或"执行中修改拓扑"。**

---

## 4. 关键洞察：为什么你的项目不需要完全动态？

论文里有个反直觉的发现：**更好的 prompt 可以弥补糟糕的拓扑，但代价是更高成本和更低鲁棒性。**

你的项目恰好证明了这一点：

1. **你的拓扑很简单**：就是一条线性的 reviewer 流水线 + Send() 并行。没有动态图生成、没有循环、没有运行时拓扑修改。

2. **但你用 prompt 和角色隔离弥补了**：
   - 4 个角色 prompt 让每个 Developer 专注不同领域
   - 子图隔离 + JSON 文件通信避免了状态混乱
   - Reviewer 的双维度审查 + 快速通道保证了质量
   - CEO 的纯代码路由避免了 LLM 调用的不确定性

3. **代价是什么？**
   - **成本高**：每个任务都要经过 write → self_check → reviewer，至少 3 次 LLM 调用
   - **鲁棒性一般**：如果图结构本身有 bug（如之前的适配器未接入），prompt 再好也没用
   - **灵活性有限**：如果任务需要 3 次以上的调研-重做循环，你的系统没有自动终止机制

**结论：你的项目选择了"简单骨架 + 强节点"的路线，这在论文分类里属于"静态为主、轻量动态为辅"。这是合理的——对于大多数开发任务，一个精心设计的静态模板 + 好的 prompt 已经足够好用。**

---

## 5. 你的项目 vs 论文核心框架 (ACG)

论文提出 ACG (Agentic Computation Graph) 统一抽象，我们用你的代码逐项对照：

### 5.1 三个层次

| 层次 | 定义 | 你的项目对应 |
|------|------|-------------|
| **ACG Template (Ḡ)** | 可复用的可执行规范 | `build_graph()` + `build_developer_subgraph()` + `build_researcher_subgraph()` |
| **Realized Graph (G_run)** | 某次运行的实际结构 | `_fanout()` 返回的 Send() 列表 + Command() 路由 |
| **Execution Trace (τ)** | 执行产生的状态序列 | `task_ledger` + JSON 文件 + streaming 事件 + `_attempts` |

### 5.2 质量-成本权衡

论文公式：
```
max E_x∼D [ E_{G_run|x} E_{τ|G_run,x} [ R(τ; x) − λC(τ) ] ]
```

你的项目隐式遵循了这个公式：

| 符号 | 你的项目体现 |
|------|-------------|
| **R(τ; x)** | Reviewer 的 score + passed 标记 |
| **C(τ)** | LLM 调用次数（每个任务 3+ 次）+ token 消耗 |
| **λ** | 隐式存在于 Reviewer 的 FAST_PASS_THRESHOLD (90分) 和 max_retries (3次) |
| **E** | 多轮对话 + checkpoint 恢复支持多次运行 |

**但你的项目没有显式优化 λ。** FAST_PASS_THRESHOLD 和 max_retries 是硬编码的，没有根据任务类型自动调整。论文里很多方法会学习 λ，你的项目暂时没有。

---

## 6. 你的项目缺失的论文方法（以及为什么可能不需要）

### 6.1 缺失：离线模板搜索

**论文方法**：AFlow 用 MCTS 搜索 typed operator graph，自动找到最优工作流结构。

**你的项目没有**：图结构是人工设计的。

**为什么可能不需要？**
- MCTS 搜索需要：typed operators（保证候选图有效）、可执行评估器、成本约束
- 你的系统只有 4 个角色、固定流程，搜索空间很小，人工设计已经足够
- 但如果未来角色数量增加到 10+，或者需要支持更多工具/验证步骤，自动搜索就会变得有价值

**什么时候需要**：当任务异构性很高，没有一个"万能模板"能覆盖所有情况时。

### 6.2 缺失：构造后执行（为每个输入生成专属图）

**论文方法**：FlowReasoner、Workflow-R1 用强化学习为每个查询生成不同的工作流。

**你的项目没有**：所有任务共用同一张图。

**为什么可能不需要？**
- 生成专属图的代价是：更大的搜索空间、更难的结构归因、需要大量训练数据
- 你的项目用 `dispatch_tasks` 字典实现了"任务级定制"——每个角色执行不同的任务，但图结构不变
- 这是一种**折中**：在固定骨架内实现任务差异化，而不是生成全新骨架

**什么时候需要**：当某些任务需要特殊拓扑（如循环验证、递归分解）而其他任务不需要时。

### 6.3 缺失：执行中编辑（运行时修改拓扑）

**论文方法**：DyFlow、AgentConductor 在部分执行后动态添加/删除节点。

**你的项目没有**：图结构在编译时就固定了。

**为什么可能不需要？**
- 执行中编辑需要：有效性检查器、停止策略、循环避免机制
- 工程复杂度极高，目前大部分工作还处于"实验室可行"阶段
- 你的项目用 retry 和 researcher 实现了"有限的动态性"——通过路由到不同节点来实现，而不是修改拓扑本身

**什么时候需要**：当环境高度不确定、工具经常失败、需要根据中间结果重组工作流时。

---

## 7. 你的项目已经做到的论文亮点

### 7.1 选择与剪枝（动态优化的最佳实践）

论文说：**"能用 selection 解决的问题不要 generation，能用 generation 解决的问题不要 editing。"**

你的项目完美践行了这一原则：

1. **Send() 扇出** = 从固定超级图里选择激活哪些分支
2. **Reviewer 快速通道** = 从固定流程里剪掉不必要的 CEO 环节
3. **熔断机制** = 在多次尝试后选择最优结果，终止搜索

这些都是**成本最低的动态形式**，不需要生成新图，不需要修改拓扑。

### 7.2 Verifier 的位置和频率

论文说：**"便宜且有语义意义的 verifier 收益最大。"**

你的项目有两个 verifier：

| Verifier | 位置 | 频率 | 成本 | 收益 |
|----------|------|------|------|------|
| **Developer 自检** | write 之后，save 之前 | 每次 write | 1 次 LLM 调用 | 早期发现知识缺口，避免无效的 reviewer 调用 |
| **Reviewer 审查** | developer 之后 | 每次 developer 完成 | 1 次 LLM 调用 | 最终质量把关，区分知识缺口 vs 代码质量 |

**注意你的自检是"前置 verifier"**——在代码保存前就检查，这比保存后再检查更高效。论文里很多方法只有最终 verifier，你的项目有双重 verifier。

### 7.3 反馈信号的多样性

论文将反馈信号分为四类：Metric-driven、Verifier-driven、Preference/Ranking、Trace-derived text。

你的项目用了**两种**：

| 信号类型 | 你的项目体现 |
|----------|-------------|
| **Verifier-driven** | Developer 自检（confidence ≥ 90）、Reviewer（score ≥ 90 快速通道） |
| **Trace-derived text** | Reviewer 的 `actionable_feedback` 和 `research_question`，直接传给 Developer 或 Researcher |

**缺失的**：Metric-driven（如 AFlow 的任务分数直接驱动搜索）、Preference/Ranking（如 Optima 的偏好学习）。

---

## 8. 结构归因：你的项目怎么知道什么起了作用？

论文里最紧迫的开放问题是**结构归因**——增益究竟来自新边、新 prompt、改变的参数，还是仅仅是更多计算？

你的项目通过以下方式实现了**轻量级归因**：

1. **角色隔离**：frontend 的问题不会影响 backend，因为它们是独立分支
2. **Reviewer 双维度分类**：区分 knowledge_gap（需要 researcher）vs code_quality（需要 developer 重做），知道问题出在哪
3. **熔断记录**：`_attempts` 记录每次尝试的 score 和 code_path，可以事后分析哪个改动提升了分数
4. **子图隔离**：Developer 的内部状态对父图不可见，避免了"状态污染"导致的归因困难

**但你的项目没有做严格的消融实验。** 你不知道：
- 如果去掉角色 prompt，分数会降多少？
- 如果去掉自检，reviewer 的负担会增加多少？
- 如果 Send() 不并行，总时间会增加多少？

论文强调这些消融实验是"最小报告协议"的一部分，你的项目暂时没有。

---

## 9. 评估方法论：你的项目缺少什么

论文提出**结构感知评价**，要求报告：

| 评估维度 | 你的项目 | 缺失 |
|----------|----------|------|
| **有效性** | Reviewer score + passed | ❌ 没有 pass@k、没有成功率统计 |
| **效率** | streaming 事件里有延迟 | ❌ 没有 token 消耗统计、没有 API 调用次数 |
| **鲁棒性** | retry 机制 | ❌ 没有工具故障注入测试、没有 API 漂移测试 |
| **图级属性** | 固定：5 节点、4 条并行边 | ❌ 没有节点数/边密度/深度的动态统计 |
| **结构变异** | Send() 动态创建分支 | ⚠️ 有，但没有记录每次运行激活了哪些分支 |
| **随机性** | checkpoint 支持 | ❌ 没有种子控制、没有重复运行、没有 canonicalization |

**你的项目在评估方面是"功能驱动"而非"研究驱动"——它能工作，但没有系统地测量工作流属性。**

---

## 10. 实践指导：你的项目下一步该怎么走？

论文给出了一个清晰的决策谱系：

```
Selection（选择/剪枝）
    ↓ 任务异构性增大
Generation（生成专属图）
    ↓ 环境交互性增强
Editing（执行中编辑）
```

**你的项目当前在 Selection 阶段，下一步该往哪走？**

### 10.1 短期（ Selection 深化）

你已经在用 Selection，但可以更精细：

1. **动态角色分配**：当前 4 个角色是固定的。CEO 可以根据任务复杂度动态决定需要哪些角色（如简单任务只需要 1 个 developer，复杂任务需要 3 个）。
   - 实现：`_build_dispatch_tasks` 已经支持只填充部分角色，但 CEO 的拆解 prompt 没有显式引导"按需分配"。

2. **自适应快速通道阈值**：当前 FAST_PASS_THRESHOLD = 90 是硬编码的。
   - 实现：可以根据任务类型调整（如安全相关任务阈值 95，原型开发阈值 85）。

3. **动态 retry 策略**：当前 max_retries = 3 是硬编码的。
   - 实现：根据任务复杂度调整（如简单任务 max_retries=1，复杂任务=5）。

### 10.2 中期（考虑 Generation）

如果任务异构性显著增大（如有的任务需要前端+后端+数据库+DevOps，有的只需要简单脚本），可以考虑：

1. **模板库 + 选择**：预定义 3-5 种模板（如"单页应用模板"、"API 服务模板"、"脚本模板"），CEO 根据任务类型选择模板，而不是生成全新结构。
   - 这比完全生成更可控，比纯选择更灵活。

2. **节点级自动配置**：引入类似 DSPy 的 prompt 自动优化，让角色 prompt 根据历史表现自动调整。

### 10.3 长期（Editing 暂不建议）

论文明确说：**执行中编辑"最难工程化"，目前大部分工作还处于实验室阶段。**

除非你的系统遇到以下场景，否则不建议投入：
- 工具经常失败，需要动态替换工具节点
- 任务目标在执行中显著变化
- 需要循环验证（如生成→测试→修复→再测试）

---

## 11. 对你项目最重要的 5 个 takeaways

### Takeaway 1: 你的图结构已经足够好，不需要追求动态生成

论文的核心结论之一是：**静态模板 + 好的 prompt 在大多数场景下已经足够好用。** 你的项目有 4 个角色、固定流程、双重 verifier，这个骨架能覆盖大多数开发任务。不要为了"动态"而动态。

### Takeaway 2: Verifier 的位置比是否存在更重要

你的 Developer 自检（write 之后、save 之前）是**前置 verifier**，这比"全部写完再检查"更高效。论文强调 verifier 应该是**便宜且有语义意义的**——你的自检用 LLM 评估置信度，符合这个原则。

**建议**：考虑在自检中加入更便宜的快速检查（如语法检查、导入检查），进一步降低 false positive。

### Takeaway 3: 反馈信号应该更丰富

当前你的反馈信号只有 Reviewer 的 `actionable_feedback` 和 `research_question`。论文展示了多种反馈类型：

- **Metric-driven**：如 AFlow 用任务 pass rate 驱动搜索
- **Preference/Ranking**：如 Optima 用偏好学习选择最优轨迹

**建议**：记录每次尝试的完整 trace（prompt、LLM 输出、审查结果），事后可以用这些数据做 offline 分析或 prompt 优化。

### Takeaway 4: 结构归因需要系统化

你不知道角色 prompt 的贡献度、自检的 ROI、并行 vs 串行的效率差异。论文强调这是"最紧迫的开放问题"。

**建议**：添加简单的 ablation 模式：
- 关闭自检，看 reviewer 的分数分布变化
- 关闭并行，看总时间变化
- 换掉角色 prompt，看质量变化

### Takeaway 5: 评估协议需要补全

论文提出的"最小报告协议"包括：节点数、边密度、延迟、token 消耗、鲁棒性测试。

**建议**：在 `task_ledger` 或 `messages` 中记录：
- 每个节点的开始/结束时间（计算延迟）
- 每次 LLM 调用的 token 数（估算成本）
- 重试次数（鲁棒性指标）

---

## 12. 总结：你的项目在论文谱系中的位置

用一句话概括：**你的项目是一个"静态骨架 + 轻量动态血液"的混合系统，位于论文分类谱系的"保守端"——但它已经包含了动态优化的核心精髓（选择/剪枝、反馈路由、verifier），只是没有追求最复杂的动态形式（生成/编辑拓扑）。**

这种选择是**明智的**。论文明确警告：

> "每一级动态性都带来额外的系统开销。"

你的项目选择了**最低复杂度的动态形式**（Selection）来解决**真实存在的问题**（任务并行、知识缺口、质量不稳定），而不是为了炫技采用 Generation 或 Editing。

**如果论文是一张地图，你的项目已经站在了"实用主义"的高地上——这里风景不错，而且不需要攀登最难爬的山。**

---

## 参考文献

- Yue et al. (2026). From Static Templates to Dynamic Runtime Graphs: A Survey of Workflow Optimization for LLM Agents. arXiv:2603.22386.
- GitHub: https://github.com/IBM/awesome-agentic-workflow-optimization

---

*报告基于原始综述论文全文及配套 awesome list，结合 Shuchiin Academy 项目实际代码分析整理。*
