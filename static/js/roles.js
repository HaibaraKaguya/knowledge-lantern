/* ═══════════════════════════════════════════════════════════════════
   演示身份 / 资料工作台能力 — 纯数据与纯函数模块（无 DOM 依赖）
   与后端 src/access 的 demo 身份约定保持一致：
     employee          普通员工         仅检索已发布公开资料
     accountant        财务会计         可提交资料进入待审核区
     finance_supervisor 财务主管        可审核 / 发布 / 拒绝会计提交的资料

   重要边界：这里的 canSubmit / canReview 只用来决定界面是否显示可用的
   控件与文案（引导体验）。它们不是权限控制。真正的授权永远由后端
   IAM（Action.KNOWLEDGE_SUBMIT / KNOWLEDGE_REVIEW）按 user_id 判定，
   前端隐藏或禁用控件不能当作权限边界。
   ═══════════════════════════════════════════════════════════════════ */
'use strict';

export const ROLE_IDS = ['employee', 'accountant', 'finance_supervisor'];

export const ROLE_META = {
  employee: {
    id: 'employee',
    label: '普通员工',
    canSubmit: false,
    canReview: false,
    panelStatus: '普通员工：仅可检索已发布的公开资料，不能提交资料，也不能审核资料。',
  },
  accountant: {
    id: 'accountant',
    label: '财务会计',
    canSubmit: true,
    canReview: false,
    panelStatus: '财务会计工作台：填写标题与正文后提交审核，资料进入待审核区；发布由财务主管负责，提交者不能审核自己的资料。',
  },
  finance_supervisor: {
    id: 'finance_supervisor',
    label: '财务主管',
    canSubmit: false,
    canReview: true,
    panelStatus: '财务主管工作台：审核、发布或拒绝会计提交的待审资料；主管不直接录入资料。',
  },
};

/** 返回指定身份的能力元数据；未知身份按 employee 降级处理。 */
export function roleMeta(roleId) {
  const id = ROLE_IDS.includes(roleId) ? roleId : 'employee';
  return ROLE_META[id];
}

export function canSubmitKnowledge(roleId) {
  return roleMeta(roleId).canSubmit;
}

export function canReviewKnowledge(roleId) {
  return roleMeta(roleId).canReview;
}
