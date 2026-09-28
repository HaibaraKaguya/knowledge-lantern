/* ═══════════════════════════════════════════════════════════════════
   Knowledge Lantern — 部门知识问答中心 前端逻辑
   视图：中心总览（业务工作模式 + 知识库指标 + 示例图表）
        知识问答（部门知识库内部检索问答工作空间，INTERNAL_RETRIEVAL）
        资料中心（会计提交 / 财务主管审核发布的工作台）
   后端契约：GET /api/paper-qa/health · POST /api/paper-qa/ask
             /api/knowledge/ingest · /api/knowledge/documents · publish/reject
   会话记录存于浏览器 localStorage；提交与审核授权由后端 IAM 判定。
   图表为原生 SVG，无第三方依赖。
   ═══════════════════════════════════════════════════════════════════ */

'use strict';
import { submitQuestion, cancelRequest, retryLastQuestion, loadSessions, renderSessions, applySessionFilter, selectSession, newSession, refreshSessions } from './chat.js';
import { updateEndpointChip, showToast, escapeHtml, toggleSidebar, closeMobileSidebar, switchView, renderEmptyState, renderHomeMetrics, renderStatGrid, renderBizSelector, selectBizMode, svgIcon } from './ui.js';
import { populateChartMode, renderChart, onChartModeChange } from './chart.js';
import { state } from './state.js';
import { BUSINESS_MODES, HEALTH_INTERVAL } from './config.js';
import { apiFetch } from './api.js';
import { initKnowledgeAdmin, bindKnowledgeAdminEvents } from './knowledge-admin.js';

/* ═══════════════════════════════════════════════════════════════════
   健康状态
   checkHealth->（setHealthChip）
  └── applyHealth
        ├── applyDatabaseCounts
        ├── applyModelStatus
        ├── renderHomeMetrics
        │     ├── extractCounts後端整理的數據
        │     └── fmtCount 格式化數據
        └── renderStatGrid
              ├── extractCounts
              └── fmtCount
   ═══════════════════════════════════════════════════════════════════ */
async function checkHealth() {
  try {
    const res = await apiFetch('/api/paper-qa/health', { silent: true });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    applyHealth(data);
  } catch (e) {
    if (e.name !== 'AbortError') {
      setHealthChip('db', 'error', '不可用');
      setHealthChip('model', 'error', '不可用');
    }
  }
}

function applyHealth(data) {
  if (!data || typeof data !== 'object') return;
  state.lastHealthData = data;
  const pqa = data.paper_qa;
  if (pqa && typeof pqa === 'object') {
    if (pqa.knowledge_db && pqa.knowledge_db.counts) applyDatabaseCounts(pqa.knowledge_db.counts);
    if (pqa.model) applyModelStatus(pqa.model);
  }
  // 兼容扁平结构（旧契约）
  if (data.database_counts) applyDatabaseCounts(data.database_counts);
  if (data.model_status) applyModelStatus(data.model_status);

  renderHomeMetrics(data);
  renderStatGrid(data);
  if (state.currentView === 'home') renderChart();
}
//知識庫可不可以用
function applyDatabaseCounts(counts) {
  if (!counts || typeof counts !== 'object') return;
  const docs = counts.documents != null ? counts.documents
    : counts.papers != null ? counts.papers
    : counts.document_count != null ? counts.document_count
    : counts.paper_count != null ? counts.paper_count
    : null;
  const chunks = counts.chunks != null ? counts.chunks
    : counts.chunk_count != null ? counts.chunk_count
    : null;
  const dbEl = document.getElementById('health-db');
  const detail = document.getElementById('health-db-detail');
  if (dbEl) dbEl.dataset.state = 'ok';
  if (detail) {
    if (docs != null && chunks != null) detail.textContent = docs + '篇 · ' + chunks + '块';
    else if (docs != null) detail.textContent = docs + '篇';
    else if (chunks != null) detail.textContent = chunks + '块';
    else detail.textContent = '可用';
  }
}

function applyModelStatus(ms) {
  const el = document.getElementById('health-model');
  const detail = document.getElementById('health-model-detail');
  if (!el || !detail) return;
  let available = null;
  if (ms && typeof ms === 'object') {
    if (typeof ms.available === 'boolean') available = ms.available;
    else if (ms.status === 'available') available = true;
    else if (ms.status === 'unavailable') available = false;
  }
  if (available === true) {
    el.dataset.state = 'ok';
    detail.textContent = ms.name || ms.model || '模型服务';
  } else if (available === false) {
    el.dataset.state = 'error';
    detail.textContent = '不可用';
    if (ms.error) el.title = String(ms.error);
  } else {
    el.dataset.state = 'error';
    detail.textContent = '未知';
  }
}

function setHealthChip(key, state, detailTxt) {
  const el = document.getElementById('health-' + key);
  const detail = document.getElementById('health-' + key + '-detail');
  if (el) el.dataset.state = state;
  if (detail) detail.textContent = detailTxt;
}
/* ═══════════════════════════════════════════════════════════════════
   业务模式卡片（领域工作空间入口）
   点击卡片 → openModeCard → 进入该领域的前端工作区（示例图表 + 示例问题）
   边界：业务数据尚未接入；后端尚未支持部门过滤，卡片只切换前端视角。
   ═══════════════════════════════════════════════════════════════════ */
function renderModeCards() {
  const grid = document.getElementById('mode-grid');
  if (!grid) return;
  grid.innerHTML = BUSINESS_MODES.map(m =>
    '<button class="mode-card" type="button" role="listitem" data-mode="' + m.id + '">'
    + '<div class="mode-top">'
    + '<span class="mode-icon" aria-hidden="true">' + svgIcon(m.icon, 20) + '</span>'
    + '<span class="mode-status ' + (m.connected ? 'ready' : 'pending') + '">'
    + (m.connected ? '已接入业务数据' : '业务数据待接入') + '</span>'
    + '</div>'
    + '<div class="mode-name">' + escapeHtml(m.name) + '</div>'
    + '<div class="mode-desc">' + escapeHtml(m.desc) + '</div>'
    + '<div class="mode-action">' + (m.connected ? '进入知识工作台 →' : '查看工作模式 →') + '</div>'
    + '</button>'
  ).join('');
}

/* 领域工作模式入口（原先在 ui.js；移到 app.js 以便同时引用 chart 与 chat，避免循环依赖） */
function openModeWorkspace(modeId) {
  const mode = BUSINESS_MODES.find(m => m.id === modeId);
  if (!mode) return;
  const ws = document.getElementById('mode-workspace');
  const nameEl = document.getElementById('mode-ws-name');
  const chips = document.getElementById('prompt-chips');
  if (ws) ws.hidden = false;
  if (nameEl) nameEl.textContent = mode.name;
  if (chips) {
    chips.innerHTML = mode.prompts.map(p =>
      '<button class="prompt-chip" type="button" data-prompt="' + escapeHtml(p) + '">'
      + escapeHtml(p) + '</button>').join('');
  }
  // 预填对应业务模式的图表示例
  const modeSel = document.getElementById('chart-mode');
  if (modeSel && modeSel.value !== modeId) {
    modeSel.value = modeId;
    onChartModeChange();
  }
  renderChart();
  if (ws) ws.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function openModeCard(modeId) {
  const mode = BUSINESS_MODES.find(m => m.id === modeId);
  if (!mode) return;
  selectBizMode(modeId);
  openModeWorkspace(modeId);
}

/* 工作模式里的示例问题：切到知识问答视图并发送 */
function runPrompt(prompt) {
  if (!prompt) return;
  switchView('knowledge');
  const input = document.getElementById('task-input');
  if (input) input.value = prompt;
  submitQuestion();
}

/* ═══════════════════════════════════════════════════════════════════
   Init — 集中事件绑定（无内联 onclick）
   ═══════════════════════════════════════════════════════════════════ */

function initIdentitySelector() {
  const select = document.getElementById('identity-select');
  if (!select) return;
  const saved = localStorage.getItem('paper_qa_user_id');
  state.currentUserId = ['employee', 'accountant', 'finance_supervisor'].includes(saved) ? saved : 'employee';
  select.value = state.currentUserId;
  select.addEventListener('change', () => {
    state.currentUserId = select.value;
    localStorage.setItem('paper_qa_user_id', state.currentUserId);
    state.currentSessionId = null;
    initKnowledgeAdmin();
    showToast('已切换为' + select.options[select.selectedIndex].text + '；新问题将按该身份校验。', '');
  });
}
function init() {
  initIdentitySelector();
  updateEndpointChip();
  populateChartMode();
  renderModeCards();
  renderBizSelector();
  renderEmptyState();
  loadSessions();
  renderSessions();
  switchView('home');
  renderChart();
  checkHealth();
  state.healthTimer = setInterval(checkHealth, HEALTH_INTERVAL);

  // 视图切换（移动端切换后自动收起抽屉）
  document.querySelectorAll('.nav-item').forEach(b => {
    b.addEventListener('click', () => {
      switchView(b.dataset.view);
      closeMobileSidebar();
    });
  });

  // 折叠侧边栏 / 移动端抽屉
  const tgl = document.getElementById('btn-toggle-sidebar');
  if (tgl) tgl.addEventListener('click', toggleSidebar);
  const closeBtn = document.getElementById('btn-close-sidebar');
  if (closeBtn) closeBtn.addEventListener('click', closeMobileSidebar);
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.addEventListener('click', closeMobileSidebar);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMobileSidebar();
  });

  // 会话
  const newBtn = document.getElementById('btn-new-session');
  if (newBtn) newBtn.addEventListener('click', newSession);
  const refreshBtn = document.getElementById('btn-refresh-sessions');
  if (refreshBtn) refreshBtn.addEventListener('click', refreshSessions);
  const search = document.getElementById('session-search');
  if (search) search.addEventListener('input', applySessionFilter);
  const list = document.getElementById('session-list');
  if (list) list.addEventListener('click', (e) => {
    const item = e.target.closest('.session-item');
    if (item) selectSession(item.dataset.sessionId);
  });

  // 问答：发送 / 取消 / Enter
  const submit = document.getElementById('submit-btn');
  if (submit) submit.addEventListener('click', submitQuestion);
  const cancel = document.getElementById('cancel-btn');
  if (cancel) cancel.addEventListener('click', cancelRequest);
  const input = document.getElementById('task-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        submitQuestion();   // 空文本时由 submitQuestion 给出提示
      }
    });
  }

  // 问答卡片事件（委托）：示例问题 chips + 重试按钮（均动态生成）
  const chat = document.getElementById('chat-area');
  if (chat) chat.addEventListener('click', (e) => {
    const chip = e.target.closest('.test-chip');
    if (chip && chip.dataset.question) {
      if (state.isRunning) { showToast('请等待当前检索完成', 'warn'); return; }
      const inputEl = document.getElementById('task-input');
      if (inputEl) inputEl.value = chip.dataset.question;
      submitQuestion();
      return;
    }
    const btn = e.target.closest('.retry-btn');
    if (btn) retryLastQuestion();
  });

  // 图表控制
  const modeSel = document.getElementById('chart-mode');
  if (modeSel) modeSel.addEventListener('change', () => { onChartModeChange(); renderChart(); });
  const granSel = document.getElementById('chart-granularity');
  if (granSel) granSel.addEventListener('change', renderChart);
  document.querySelectorAll('#chart-type-seg .seg-btn').forEach(b => {
    b.addEventListener('click', () => {
      state.currentChartType = b.dataset.type;
      document.querySelectorAll('#chart-type-seg .seg-btn').forEach(x => {
        const on = x === b;
        x.classList.toggle('is-active', on);
        x.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      renderChart();
    });
  });
  const gen = document.getElementById('btn-gen-chart');
  if (gen) gen.addEventListener('click', renderChart);

  // 业务模式卡片
  const grid = document.getElementById('mode-grid');
  if (grid) grid.addEventListener('click', (e) => {
    const card = e.target.closest('.mode-card');
    if (card) openModeCard(card.dataset.mode);
  });

  // 知识工作台业务模式选择器（五个业务模式按钮）
  const bizWrap = document.getElementById('biz-selector-btns');
  if (bizWrap) bizWrap.addEventListener('click', (e) => {
    const btn = e.target.closest('.biz-btn');
    if (btn) selectBizMode(btn.dataset.mode);
  });

  // 工作模式示例问题
  const chips = document.getElementById('prompt-chips');
  if (chips) chips.addEventListener('click', (e) => {
    const chip = e.target.closest('.prompt-chip');
    if (chip) runPrompt(chip.dataset.prompt);
  });

  // 资料中心：静态按钮只绑定一次
  bindKnowledgeAdminEvents();
}

init();
initKnowledgeAdmin();
