/* 配置模块：业务模式、检索上下文与前端常量。
   全部为前端展示配置，不改变后端 API 契约。 */
'use strict';

/* 可检索语料的正式名称：测试期种子知识。
   它是项目自建的合成 / 整理测试语料，不是任何真实公司的制度或经营数据。 */
export const RETRIEVAL_CONTEXT_LABEL = '测试期种子知识';
export const RETRIEVAL_CONTEXT_NOTE =
  '当前检索上下文为测试期种子知识（部门制度与业务流程的合成 / 整理语料），非真实公司数据。';

/* 后端 /api/paper-qa/ask 目前不接受适用部门（applicable_departments）过滤参数。
   前端因此在任何提示中都不得声称部门过滤已生效 —— 仅展示为“待接线”。 */
export const DEPT_FILTER_WIRED = false;
export const DEPT_FILTER_LABEL = '部门过滤';
export const DEPT_FILTER_PENDING_NOTE =
  '业务模式只切换助手视角与会话标题；后端检索接口尚未支持部门过滤（applicable_departments）参数，'
  + '因此检索证据仍为当前身份授权范围，不按部门筛选。';

export const BUSINESS_MODES = [
  {
    id: 'operations', icon: 'chart', name: '经营分析', desc: '经营指标口径与趋势解读',
    metric: '营收（示例值 · 万元）', base: 1180, amp: 260, decimals: 0, connected: false,
    agentInstruction: '你是一位专业的经营分析顾问。请从经营目标、指标口径、趋势、风险与可执行建议五个角度解读问题；信息不足时明确需要补充的数据。',
    prompts: ['近三个季度的营收趋势怎么看？', '如何建立经营分析的核心指标体系？'],
  },
  {
    id: 'finance', icon: 'shield', name: '财务合规', desc: '合规检查与风险提示',
    metric: '检查通过率（示例值 · %）', base: 92.5, amp: 4.2, decimals: 1, connected: false,
    agentInstruction: '你是一位专业的财务合规顾问。请识别风险点、适用流程、所需凭证与控制措施；不要把一般性建议当作正式法律或审计结论。',
    prompts: ['如何构建财务合规检查清单？', '如何识别日常报销流程的常见风险？'],
  },
  {
    id: 'hr', icon: 'people', name: '人力资源', desc: '人员结构与流程协同',
    metric: '在岗人数（示例值 · 人）', base: 318, amp: 28, decimals: 0, connected: false,
    agentInstruction: '你是一位专业的人力资源专家。请结合招聘、组织发展、绩效、员工关系和劳动合规给出结构化建议；说明前提条件、潜在风险与下一步行动。',
    prompts: ['招聘流程的关键节点有哪些？', '如何设计一场有效的绩效沟通？'],
  },
  {
    id: 'project_operations', icon: 'rocket', name: '项目与运营', desc: '项目进度与运营指标',
    metric: '里程碑达成率（示例值 · %）', base: 86, amp: 9, decimals: 1, connected: false,
    agentInstruction: '你是一位专业的项目与运营顾问。请围绕目标、范围、里程碑、风险、责任人与复盘机制给出可落地的方案。',
    prompts: ['项目管理中里程碑如何设定？', '怎样做一次能落地的项目复盘？'],
  },
  {
    id: 'administration', icon: 'building', name: '行政', desc: '行政事务、制度与服务协同',
    metric: '服务满意度（示例值 · %）', base: 94, amp: 3.5, decimals: 1, connected: false,
    agentInstruction: '你是一位专业的行政管理顾问。请从制度流程、服务体验、资源协调、通知沟通和风险防范角度给出清晰、可执行的建议。',
    prompts: ['如何优化会议室预约与使用流程？', '如何写一份清晰的行政通知？'],
  },
];

/* ═══════════════════════════════════════════════════════════════════
   配置 — 127.0.0.1 绕过浏览器代理，首次失败自动切 localhost
   ═══════════════════════════════════════════════════════════════════ */

export const API_BASE = 'http://127.0.0.1:8000';
export const FALLBACK_API_BASE = 'http://localhost:8000';
export let activeApiBase = API_BASE;
export function setActiveApiBase(value) { activeApiBase = value; }
export const HEALTH_INTERVAL = 20000;          // 健康状态轮询间隔
export const LS_KEY = 'knowledge-lantern.paperqa.sessions.v1';
export const MAX_SESSIONS = 50;                // 本地保留的最大会话数
export const ROUTE_FIXED = 'INTERNAL_RETRIEVAL';
export const GRAN_LABELS = { day: '按日', week: '按周', month: '按月', quarter: '按季' };

/* 路由标识 → 界面文案（纯函数，供 ui/chat 共用，避免 ui→chat 循环依赖） */
export function routeLabel(route) {
  if (route === 'INTERNAL_RETRIEVAL') return '知识库内部检索';
  if (route === 'DIRECT') return '直接回答';
  return route || '知识库内部检索';
}

/* 空状态示例问题（基于当前可检索的测试期种子知识：部门制度与业务流程语料） */
export const EXAMPLE_QUESTIONS = [
  '财务合规的“月末结账”处理流程是什么？',
  '经营分析的“经营日报”报表如何查询与解读？',
  '人力资源遇到“报错排查”类问题应如何排查？',
  '项目与运营的“业务流程”标准步骤有哪些？',
  '行政的“参数配置”应遵循什么口径？',
  '提交的资料在审核发布后如何进入检索索引？',
];
