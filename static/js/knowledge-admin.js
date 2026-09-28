/* ═══════════════════════════════════════════════════════════════════
   资料中心工作台 —— 会计提交 / 财务主管审核
   - app.js 在启动与身份切换时调用 initKnowledgeAdmin()；
   - 界面控件按所选身份启用 / 禁用并给出文案引导；
   - 前端“隐藏 / 禁用”只是体验引导，不是权限控制 —— 提交与审核授权
     始终由后端 IAM（Action.KNOWLEDGE_SUBMIT / KNOWLEDGE_REVIEW）
     按 user_id 校验，伪造身份或绕过界面都会被服务端拒绝。
   - 适用部门（applicable_departments）尚未接入后端，前端只做只读待接线说明，
     绝不提交该字段、也不声称部门过滤已生效。
   ═══════════════════════════════════════════════════════════════════ */
'use strict';
import { state } from './state.js';
import { apiFetch } from './api.js';
import { escapeHtml, showToast } from './ui.js';
import { roleMeta } from './roles.js';

const INGEST_FIELDS = [
  'ingest-document-id',
  'ingest-title',
  'ingest-classification',
  'ingest-department',
  'ingest-content',
];
const INGEST_BTN = 'btn-ingest';
const INGEST_SPINNER = 'ingest-spinner';
const INGEST_TEXT = 'ingest-text';
const REFRESH_BTN = 'btn-refresh-docs';

function el(id) {
  return document.getElementById(id);
}

function setDisabled(id, disabled) {
  const node = el(id);
  if (node) node.disabled = !!disabled;
}

function setStatus(message, status) {
  const node = el('knowledge-admin-status');
  if (!node) return;
  node.textContent = message || '';
  if (status) node.dataset.state = status;
  else delete node.dataset.state;
}

/** 提交按钮的加载 / 禁用反馈（含 aria-busy 与可见文案）。 */
function setIngestBusy(busy) {
  const btn = el(INGEST_BTN);
  const spinner = el(INGEST_SPINNER);
  const text = el(INGEST_TEXT);
  if (btn) {
    btn.disabled = !!busy;
    btn.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  if (spinner) spinner.classList.toggle('active', !!busy);
  if (text) text.textContent = busy ? '提交中…' : '提交审核';
}

/** 按身份配置工作台：先把控件恢复可用，再按角色限制，避免角色切换后残留禁用态。 */
function applyRoleUI(meta) {
  // 1) 恢复全部可写控件
  INGEST_FIELDS.forEach(id => setDisabled(id, false));
  setIngestBusy(false);
  setDisabled(REFRESH_BTN, false);

  // 2) 按角色限制
  const canSubmit = meta.canSubmit;
  const canWorkQueue = canSubmit || meta.canReview;
  INGEST_FIELDS.forEach(id => setDisabled(id, !canSubmit));
  setDisabled(INGEST_BTN, !canSubmit);
  setDisabled(REFRESH_BTN, !canWorkQueue);

  setStatus(meta.panelStatus, canSubmit || meta.canReview ? '' : 'pending');
}

function pendingListEmptyNote(meta) {
  if (meta.canSubmit) return '暂无待审核资料 —— 你可填写上方表单提交新资料。';
  if (meta.canReview) return '暂无待审核资料 —— 会计提交后，资料会出现在这里等待审核。';
  return '待审核队列仅对「财务会计 / 财务主管」开放；普通员工仅可检索已发布资料。';
}

async function loadQueue(meta) {
  const list = el('knowledge-admin-list');
  if (!list) return;

  if (!meta.canSubmit && !meta.canReview) {
    list.innerHTML = '<div class="empty-list">' + escapeHtml(pendingListEmptyNote(meta)) + '</div>';
    return;
  }
  const pending = el(REFRESH_BTN);
  if (pending) { pending.disabled = true; pending.setAttribute('aria-busy', 'true'); }
  let res;
  try {
    res = await apiFetch('/api/knowledge/documents?status=pending_review');
    if (!res.ok) throw new Error(await res.text());
  } catch (error) {
    setStatus('加载待审资料失败：' + (error.message || '网络错误'), 'error');
    if (pending) { pending.disabled = false; pending.setAttribute('aria-busy', 'false'); }
    return;
  }
  if (pending) { pending.disabled = false; pending.setAttribute('aria-busy', 'false'); }
  const docs = (await res.json()).documents || [];
  if (!docs.length) {
    list.innerHTML = '<div class="empty-list">' + escapeHtml(pendingListEmptyNote(meta)) + '</div>';
    return;
  }
  list.innerHTML = docs.map(d => {
    const title = escapeHtml((d.title || d.document_id) + (d.submitter_id ? ' · 提交者 ' + d.submitter_id : ''));
    if (meta.canReview) {
      return '<div class="admin-doc-row"><span>' + escapeHtml(d.document_id) + ' · ' + title + '</span>'
        + '<span class="admin-doc-actions">'
        + '<button class="btn btn-primary btn-sm" type="button" data-publish="' + escapeHtml(d.document_id) + '" aria-label="发布资料 ' + escapeHtml(d.document_id) + '">发布</button>'
        + '<button class="btn btn-ghost btn-sm" type="button" data-reject="' + escapeHtml(d.document_id) + '" aria-label="拒绝资料 ' + escapeHtml(d.document_id) + '">拒绝</button>'
        + '</span></div>';
    }
    return '<div class="admin-doc-row"><span>' + escapeHtml(d.document_id) + ' · ' + title + '</span>'
      + '<span class="muted">待主管审核</span></div>';
  }).join('');
}

async function reviewDoc(documentId, action, button) {
  const meta = roleMeta(state.currentUserId);
  if (!meta.canReview) {
    setStatus('当前身份无审核权限；该操作已交由后端按 user_id 校验并拒绝。', 'error');
    return;
  }
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  try {
    const res = await apiFetch('/api/knowledge/documents/' + encodeURIComponent(documentId) + '/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: state.currentUserId, reviewer_id: state.currentUserId, note: '' }),
    });
    if (res.ok) {
      setStatus(action === 'publish' ? '已发布，资料进入检索索引。' : '已拒绝该资料。', 'ok');
    } else {
      setStatus('操作失败：' + (await res.text()), 'error');
    }
    await loadQueue(meta);
  } catch (error) {
    setStatus('操作失败：' + (error.message || '网络错误'), 'error');
  } finally {
    if (button) { button.disabled = false; button.setAttribute('aria-busy', 'false'); }
  }
}

async function onIngest() {
  const meta = roleMeta(state.currentUserId);
  if (!meta.canSubmit) {
    showToast('当前身份无资料提交权限（后端按 user_id 校验）', 'warn');
    setStatus('只有财务会计可以提交资料；普通员工与财务主管提交会被后端拒绝。', 'error');
    return;
  }
  const documentId = (el('ingest-document-id') && el('ingest-document-id').value.trim()) || '';
  const title = (el('ingest-title') && el('ingest-title').value.trim()) || '';
  const content = (el('ingest-content') && el('ingest-content').value) || '';
  const classification = (el('ingest-classification') && el('ingest-classification').value) || 'internal';
  const department = (el('ingest-department') && el('ingest-department').value) || '财务合规';

  if (!content.trim()) {
    setStatus('提交未完成：请先填写资料正文（content 不能为空）。', 'error');
    if (el('ingest-content')) el('ingest-content').focus();
    return;
  }
  if (!title) {
    setStatus('提交未完成：请先填写资料标题。', 'error');
    if (el('ingest-title')) el('ingest-title').focus();
    return;
  }
  setIngestBusy(true);
  setStatus('正在提交审核…', 'pending');
  try {
    const res = await apiFetch('/api/knowledge/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: state.currentUserId,
        document_id: documentId || undefined,
        title,
        content,
        classification,
        department,
        source: '资料中心工作台',
        version: '1',
      }),
    });
    if (res.ok) {
      setStatus('已提交审核，资料进入待审核区（财务主管发布后才会进入检索索引）。', 'ok');
      const t = el('ingest-title'); if (t) t.value = '';
      const c = el('ingest-content'); if (c) c.value = '';
    } else {
      setStatus('提交失败：' + (await res.text()), 'error');
    }
    await loadQueue(roleMeta(state.currentUserId));
  } catch (error) {
    setStatus('提交失败：' + (error.message || '网络错误'), 'error');
  } finally {
    setIngestBusy(false);
  }
}

/** 每次身份切换都调用：应用角色控件状态并刷新待审队列。 */
export async function initKnowledgeAdmin() {
  const panel = el('knowledge-admin-panel');
  if (!panel) return;
  panel.hidden = false;
  const meta = roleMeta(state.currentUserId);
  applyRoleUI(meta);
  await loadQueue(meta);
}

/** 在 DOM 里绑定一次静态按钮事件（模块加载后由 app.js 触发）。 */
export function bindKnowledgeAdminEvents() {
  const ingestBtn = el(INGEST_BTN);
  if (ingestBtn && !ingestBtn.dataset.bound) {
    ingestBtn.dataset.bound = '1';
    ingestBtn.addEventListener('click', onIngest);
  }
  const refreshBtn = el(REFRESH_BTN);
  if (refreshBtn && !refreshBtn.dataset.bound) {
    refreshBtn.dataset.bound = '1';
    refreshBtn.addEventListener('click', () => initKnowledgeAdmin());
  }
  const list = el('knowledge-admin-list');
  if (list && !list.dataset.bound) {
    list.dataset.bound = '1';
    list.addEventListener('click', (e) => {
      const publish = e.target.closest('[data-publish]');
      if (publish) { reviewDoc(publish.dataset.publish, 'publish', publish); return; }
      const reject = e.target.closest('[data-reject]');
      if (reject) { reviewDoc(reject.dataset.reject, 'reject', reject); }
    });
  }
}
