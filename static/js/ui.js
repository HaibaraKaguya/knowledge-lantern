/* UI 模块：后续从 app.js 拆分。 */
'use strict';
import {
  activeApiBase, BUSINESS_MODES, ROUTE_FIXED, EXAMPLE_QUESTIONS, routeLabel,
  RETRIEVAL_CONTEXT_LABEL, DEPT_FILTER_LABEL, DEPT_FILTER_PENDING_NOTE,
} from './config.js';
import { state } from './state.js';
import { extractCounts, fmtCount, formatScore, fmtDuration, normalizeSource } from './utils.js';

/* ═══════════════════════════════════════════════════════════════════
   内联 SVG 图标（不使用 emoji 作结构性图标；无外部依赖）
   ═══════════════════════════════════════════════════════════════════ */
const ICON_PATHS = {
  chart: '<polyline points="4 16.5 9.5 11 13.5 14 20 6.5"/><line x1="4" y1="20" x2="20" y2="20"/>',
  shield: '<path d="M12 3.5 19 6.5v5c0 4.4-2.9 7.4-7 9-4.1-1.6-7-4.6-7-9v-5z"/><path d="m9 12 2 2 4-4"/>',
  people: '<circle cx="9.5" cy="9" r="3"/><path d="M4 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><path d="M16 6.8a3 3 0 0 1 0 5.4"/>',
  rocket: '<path d="M12 3c3 2 5 5.2 5 9l-2.5 3.2h-5L7 12c0-3.8 2-7 5-9z"/><circle cx="12" cy="10" r="1.6"/><path d="M9.5 18 12 21l2.5-3"/>',
  building: '<rect x="4" y="4" width="7" height="16" rx="1"/><rect x="12.5" y="9.5" width="7.5" height="10.5" rx="1"/><line x1="7" y1="8" x2="8" y2="8"/><line x1="7" y1="12" x2="8" y2="12"/><line x1="15.5" y1="13.5" x2="16.5" y2="13.5"/>',
  book: '<path d="M4.5 5.5A2.5 2.5 0 0 1 7 3h12v15H7a2.5 2.5 0 0 0-2.5 2.5z"/><path d="M4.5 20.5A2.5 2.5 0 0 1 7 18h12v3H7a2.5 2.5 0 0 1-2.5-2.5z"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  answer: '<path d="M5 4.5h14v11H9.5L5 19z"/><path d="M8.5 9h7"/><path d="M8.5 12h4"/>',
  cancel: '<circle cx="12" cy="12" r="9"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/>',
  failed: '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  refresh: '<polyline points="21 5 21 10 16 10"/><path d="M19.5 15a8 8 0 1 1-1.9-8.4L21 9"/>',
  chat: '<path d="M4.5 5.5h15v10.5H9.5L4.5 20z"/><path d="M8.5 9.5h7"/><path d="M8.5 12.5h4"/>',
  inbox: '<path d="M4 13h4l1.5 2.5h5L16 13h4"/><path d="M6.5 5.5h11l2.5 7.5v6h-16v-6z"/>',
};

export function svgIcon(name, size) {
  const path = ICON_PATHS[name];
  if (!path) return '';
  const s = size || 16;
  return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    + ' stroke-linejoin="round" aria-hidden="true" focusable="false">' + path + '</svg>';
}

export function bizIcon(name, size) {
  return svgIcon(name, size || 15);
}

/*更新api接口，顯示在endpoint-chip元素中*/
export function updateEndpointChip() {
  const chip = document.getElementById('endpoint-chip');
  if (chip) chip.textContent = 'API · ' + activeApiBase.replace(/^https?:\/\//, '');
}
/* 显示一个 toast 消息，根據時間*/
export function showToast(msg, type) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = (type || '') + ' show';
  setTimeout(() => { t.className = ''; }, 5000);
}
/*添加一个新的会话卡片到会话列表中，返回新卡片的 DOM 元素*/
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}
/*對話題題目進行提取，去掉開頭的請求詞，去掉結尾的疑問詞，限制長度，返回一個簡短的主題*/
export function extractTopic(text) {
  let firstLine = text.split(/\n/)[0].replace(/^#+\s*/, '').trim();
  if (!firstLine) firstLine = text.replace(/\n/g, ' ').slice(0, 100);
  let t = firstLine.replace(/^(帮我|给我|请|麻烦|帮忙)\s*/, '').trim();
  t = t.split(/[，,。.、；;：:\n\r（(．-]/)[0].trim();
  if (t.length > 28) t = t.slice(0, 26) + '…';
  t = t.replace(/([吗呢吧嘛]$)|([！!？?]+$)/g, '').trim();
  t = t.replace(/[了过]$/, '');
  return t || '新会话';
}
export function setStatus(status, text) {
  const dot = document.getElementById('status-dot');
  if (dot) dot.className = 'status-dot ' + status;
  const el = document.getElementById('status-text');
  if (el) el.textContent = text || status;
}
// 切换侧边栏：桌面折叠宽度，移动端抽屉显隐
function isMobileNav() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(max-width: 900px)').matches;
}
export function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  if (!sb) return;
  const btn = document.getElementById('btn-toggle-sidebar');
  if (isMobileNav()) {
    const open = !sb.classList.contains('mobile-open');
    sb.classList.toggle('mobile-open', open);
    document.body.classList.toggle('sidebar-open', open);
    const backdrop = document.getElementById('sidebar-backdrop');
    if (backdrop) backdrop.hidden = !open;
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    return;
  }
  const collapsed = sb.classList.toggle('collapsed');
  document.body.classList.toggle('sidebar-open', !collapsed);
  if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}
export function closeMobileSidebar() {
  const sb = document.getElementById('sidebar');
  if (!sb || !sb.classList.contains('mobile-open')) return;
  sb.classList.remove('mobile-open');
  document.body.classList.remove('sidebar-open');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.hidden = true;
  const btn = document.getElementById('btn-toggle-sidebar');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}
// 滚动聊天区域到底部
export function scrollToBottom() {
  const chat = document.getElementById('chat-area');
  if (chat) chat.scrollTop = chat.scrollHeight;
}
// 获取聊天内容的目标容器，优先返回 chat-inner，如果不存在则返回 chat-area，取代裏面原有的東西
export function getChatTarget() {
  const chat = document.getElementById('chat-area');
  const inner = document.getElementById('chat-inner');
  const target = inner || chat;
  const empty = target.querySelector('.empty-state');
  if (empty) empty.remove();
  return target;
}
export function renderEmptyState(container) {
  const chat = container || document.getElementById('chat-area');
  if (!chat) return;
  chat.innerHTML = '<div class="chat-inner" id="chat-inner"><div class="empty-state">'
    + '<div class="logo-ring" aria-hidden="true">' + svgIcon('book', 30) + '</div>'
    + '<h3>部门知识问答中心 · 内部检索问答</h3>'
    + '<p class="tagline">权限先于检索：未授权资料不会进入检索证据。当前可检索知识为<b>'
    + RETRIEVAL_CONTEXT_LABEL + '</b>（部门制度与业务流程的合成 / 整理语料，非真实公司数据）；'
    + '部门资料由「会计提交 → 主管审核 → 发布入库」后进入检索索引。</p>'
    + '<div class="empty-route">'
    + '<span class="route-badge INTERNAL_RETRIEVAL"><span class="dot"></span>知识库内部检索</span>'
    + '<span class="empty-route-note">固定内部检索路由 · 模型服务依据检索证据作答</span>'
    + '</div>'
    + '<div class="empty-scope">'
    + '<span class="scope-chip ok">检索上下文 · ' + escapeHtml(RETRIEVAL_CONTEXT_LABEL) + '</span>'
    + '<span class="scope-chip pending">' + escapeHtml(DEPT_FILTER_LABEL) + ' · 待接线</span>'
    + '</div>'
    + '<div class="test-questions">'
    + '<div class="test-label">试试这些问题：</div>'
    + '<div class="test-chip-wrap" id="test-chips">'
    + EXAMPLE_QUESTIONS.map(q =>
      '<button class="test-chip" type="button" data-question="' + escapeHtml(q) + '"'
      + ' aria-label="提问：' + escapeHtml(q) + '">' + escapeHtml(q) + '</button>').join('')
    + '</div>'
    + '</div></div></div>';
}
//在三個視圖中來回變換
export function switchView(view) {
  state.currentView = view;
  const home = document.getElementById('view-home');
  const know = document.getElementById('view-knowledge');
  const ingest = document.getElementById('view-ingest');
  const cHome = document.getElementById('context-home');
  const cSess = document.getElementById('context-sessions');
  const badge = document.getElementById('fixed-route-badge');
  const crumbHome = document.getElementById('crumb-home');
  const crumbCur = document.getElementById('crumb-current');

  if (view === 'home') {
    if (home) home.hidden = false;
    if (know) know.hidden = true;
    if (ingest) ingest.hidden = true;
    if (cHome) cHome.hidden = false;
    if (cSess) cSess.hidden = true;
    if (badge) badge.hidden = true;
    if (crumbHome) crumbHome.textContent = '首页总览';
    if (crumbCur) crumbCur.textContent = '部门知识问答中心';
    if (state.lastHealthData) {
      renderHomeMetrics(state.lastHealthData);
      renderStatGrid(state.lastHealthData);
    }
  } else if (view === 'knowledge') {
    if (home) home.hidden = true;
    if (know) know.hidden = false;
    if (ingest) ingest.hidden = true;
    if (cHome) cHome.hidden = true;
    if (cSess) cSess.hidden = false;
    if (badge) badge.hidden = false;
    if (crumbHome) crumbHome.textContent = '知识问答';
    if (crumbCur) {
      const s = state.sessions.find(x => x.id === state.currentSessionId);
      crumbCur.textContent = (s && s.title) ? s.title : '知识问答工作空间';
    }
  } else {
    if (home) home.hidden = true;
    if (know) know.hidden = true;
    if (ingest) ingest.hidden = false;
    if (cHome) cHome.hidden = true;
    if (cSess) cSess.hidden = true;
    if (badge) badge.hidden = true;
    if (crumbHome) crumbHome.textContent = '资料中心';
    if (crumbCur) crumbCur.textContent = '会计提交 / 主管审核工作台';
  }
  document.querySelectorAll('.nav-item').forEach(b => {
    b.classList.toggle('is-active', b.dataset.view === view);
  });
}
//將後端輸出的數據json化細小化
export function renderHomeMetrics(data) {
  const box = document.getElementById('home-metrics');
  if (!box) return;
  const c = extractCounts(data);
  const mk = (label, value) =>
    '<div class="mini-stat"><span class="mini-label">' + escapeHtml(label) + '</span>'
    + '<span class="mini-value' + (value === '—' ? ' off' : '') + '">' + escapeHtml(String(value)) + '</span></div>';
  const modelTxt = c.model ? (c.model.status === 'available' ? '可用' : '不可用') : '—';
  box.innerHTML = [
    mk('知识文档', fmtCount(c.kdb && c.kdb.documents)),
    mk('知识分块', fmtCount(c.kdb && c.kdb.chunks)),
    mk('会话轮次', fmtCount(c.turns)),
    mk('审计事件', fmtCount(c.events)),
    mk('模型服务', modelTxt),
  ].join('');
}
//將後端輸出的數據json化包括主要内容
export function renderStatGrid(data) {
  const grid = document.getElementById('stat-grid');
  if (!grid) return;
  const c = extractCounts(data);
  const mk = (label, value, sub, cls) =>
    '<div class="stat-card ' + (cls || '') + '">'
    + '<div class="stat-value' + (value === '—' ? ' off' : '') + '">' + escapeHtml(String(value)) + '</div>'
    + '<div class="stat-label">' + escapeHtml(label) + '</div>'
    + (sub ? '<div class="stat-sub">' + escapeHtml(sub) + '</div>' : '')
    + '</div>';
  const model = c.model;
  const modelCls = !model ? 'is-warn'
    : model.status === 'available' ? 'is-good'
    : model.status === 'unavailable' ? 'is-bad' : 'is-warn';
  grid.innerHTML = [
    mk('知识文档', fmtCount(c.kdb && c.kdb.documents), 'knowledge.db · documents'),
    mk('知识分块', fmtCount(c.kdb && c.kdb.chunks), 'knowledge.db · chunks'),
    mk('会话轮次', fmtCount(c.turns), 'checkpoints_db.turns'),
    mk('审计事件', fmtCount(c.events), 'audit_db.events'),
    mk('模型服务', model ? (model.status === 'available' ? '可用' : '不可用') : '—', model && model.name ? model.name : '等待探测', modelCls),
  ].join('');
}
//用於修改是否精排reranker
export function renderSources(sources, container, opts) {
  if (!sources || !sources.length) return 0;
  const list = document.createElement('div');
  list.className = 'sources-list';
  const header = document.createElement('div');
  header.className = 'sources-header';
  const rr = (opts && opts.reranker) || {};
  let rrNote = '';
  if (rr.available === false) {
    rrNote = '<span class="rerank-note">精排未生效（' + escapeHtml(rr.status || 'degraded') + '）· 保持 RRF 顺序</span>';
  } else if (rr.status === 'active') {
    rrNote = '<span class="rerank-note active">精排已启用 · 已按精排分重排</span>';
  }
  header.innerHTML = '<span>检索证据（' + sources.length + ' 条来源）</span>'
    + rrNote
    + '<span class="sources-hint">点击来源卡片展开原文片段</span>';
  list.appendChild(header);

  sources.forEach((s, i) => {
    const src = normalizeSource(s, i);
    if (!src) return;
    const scoreTxt = formatScore(src.score);
    const scoreCls = src.score == null ? 'none' : '';
    const chunkTxt = src.chunk_id ? '<span class="source-chunk">' + escapeHtml(src.chunk_id) + '</span>' : '';
    const headingTxt = src.heading ? escapeHtml(src.heading) : '（无章节标题）';
    const pageTxt = src.page ? ' · p' + escapeHtml(String(src.page)) : '';

    const el = document.createElement('details');
    el.className = 'source-card';
    el.innerHTML =
      '<summary class="source-card-head">'
      + '<span class="source-rank">' + escapeHtml(src.rank) + '</span>'
      + '<span class="source-main">'
      + '<span class="source-title">' + escapeHtml(src.title) + '</span>'
      + '<span class="source-heading">' + headingTxt + pageTxt + '</span>'
      + '</span>'
      + '<span class="source-score ' + scoreCls + '">' + scoreTxt + '</span>'
      + chunkTxt
      + '<span class="source-caret" aria-hidden="true">▸</span>'
      + '</summary>'
      + '<div class="source-body">'
      + '<div class="source-fields">'
      + '<span><b>资料</b>' + escapeHtml(src.title) + '</span>'
      + (src.heading ? '<span><b>章节</b>' + escapeHtml(src.heading) + '</span>' : '')
      + (src.chunk_id ? '<span><b>chunk</b>' + escapeHtml(src.chunk_id) + '</span>' : '')
      + '<span><b>分数</b>' + scoreTxt + '</span>'
      + (src.page ? '<span><b>页码</b>' + escapeHtml(String(src.page)) + '</span>' : '')
      + (src.match_type ? '<span><b>命中</b>' + escapeHtml(src.match_type) + '</span>' : '')
      + (Object.keys(src.branch_ranks || {}).length
        ? '<span><b>分支排名</b>BM25 #' + escapeHtml(String(src.branch_ranks.bm25 || '—'))
          + ' · BGE-M3 #' + escapeHtml(String(src.branch_ranks.vector || '—')) + '</span>' : '')
      + (src.rrf_rank != null
        ? '<span><b>RRF位次</b>#' + escapeHtml(String(src.rrf_rank)) + '</span>' : '')
      + (src.reranker_score != null
        ? '<span><b>精排分</b>' + formatScore(src.reranker_score) + '</span>' : '')
      + (src.final_rank != null
        ? '<span><b>最终位次</b>#' + escapeHtml(String(src.final_rank)) + '</span>' : '')
      + '</div>'
      + '<pre class="source-excerpt">' + escapeHtml(src.text || '（该来源无文本片段）') + '</pre>'
      + '</div>';
    list.appendChild(el);
  });

  container.appendChild(list);
  return sources.length;
}

export function renderRelations(relations, container) {
  if (!relations || !relations.length) return 0;
  const section = document.createElement('div');
  section.className = 'graph-relations';
  section.innerHTML = '<div class="sources-header"><span>图关系证据（'
    + relations.length + ' 条边）</span><span class="sources-hint">实体 → 关系 → 实体</span></div>';
  relations.forEach((rel) => {
    const card = document.createElement('div');
    card.className = 'graph-edge-card';
    card.innerHTML = '<div class="graph-triple">'
      + '<span class="graph-node">' + escapeHtml(rel.subject || '未知实体') + '</span>'
      + '<span class="graph-predicate">— ' + escapeHtml(rel.predicate || 'RELATED_TO') + ' →</span>'
      + '<span class="graph-node">' + escapeHtml(rel.object || '未知实体') + '</span>'
      + '</div>'
      + '<div class="graph-provenance">来源：' + escapeHtml(rel.title || rel.document_id || '未知资料')
      + (rel.evidence_section ? ' · ' + escapeHtml(rel.evidence_section) : '') + '</div>'
      + (rel.evidence_quote ? '<div class="graph-evidence">' + escapeHtml(rel.evidence_quote) + '</div>' : '');
    section.appendChild(card);
  });
  container.appendChild(section);
  return relations.length;
}
export function metaStrip(timings, route) {
  let html = '<div class="answer-meta">'
    + '<span class="route-badge INTERNAL_RETRIEVAL"><span class="dot"></span>'
    + escapeHtml(routeLabel(route)) + '</span>'
    + '<span class="meta-chip">检索上下文 · ' + escapeHtml(RETRIEVAL_CONTEXT_LABEL) + '</span>'
    + '<span class="meta-chip pending">' + escapeHtml(DEPT_FILTER_LABEL) + ' · 待接线</span>';
  if (timings && timings.retrieval_ms != null) html += '<span class="meta-chip">检索 ' + escapeHtml(fmtDuration(timings.retrieval_ms)) + '</span>';
  const genMs = timings && (timings.generation_ms != null ? timings.generation_ms : timings.answer_ms);
  if (genMs != null) html += '<span class="meta-chip">模型 ' + escapeHtml(fmtDuration(genMs)) + '</span>';
  if (timings && timings.total_ms != null) html += '<span class="meta-chip">总计 ' + escapeHtml(fmtDuration(timings.total_ms)) + '</span>';
  html += '</div>';
  return html;
}
export function renderAssistantCard(data, target) {
  const card = document.createElement('div');
  card.className = 'card assistant';

  const answer = data.answer || '';
  const route = data.route || ROUTE_FIXED;
  const modelStatus = data.model_status || data.model || {};
  const timings = data.timings || {};
  const sources = data.sources || [];
  const relations = data.relations || [];

  let html = '<div class="card-header">' + svgIcon('answer', 14) + '<span>回答</span></div>';
  html += metaStrip(timings, route);
  if (data.retrieval_query) {
    html += '<div class="evidence-note"><b>查询理解：</b>'
      + escapeHtml(data.retrieval_query) + '</div>';
  }

  const Aiunavilable = (typeof modelStatus.available === 'boolean' && modelStatus.available === false)
    || modelStatus.status === 'unavailable';
  if (Aiunavilable) {
    html += '<div class="warn-banner">' + svgIcon('failed', 14) + '<span>大模型当前不可用'
      + (modelStatus.error ? '：' + escapeHtml(modelStatus.error) : '')
      + '。本次未生成回答，下方仍展示检索到的知识库证据。</span></div>';
  }

  if (answer) {
    html += '<div class="answer-body">' + escapeHtml(answer) + '</div>';
  } else if (Aiunavilable) {
    html += '<div class="answer-body empty">（大模型不可用，无回答生成）</div>';
  } else {
    html += '<div class="answer-body empty">未生成回答。</div>';
  }

  card.innerHTML = html;

  const sourcesEl = document.createElement('div');
  sourcesEl.className = 'sources-section';
  if (sources.length) {
    renderSources(sources, sourcesEl, { reranker: data.reranker });
  } else if (Aiunavilable) {
    sourcesEl.innerHTML = '<div class="evidence-note">本次没有可展示的检索证据（大模型不可用导致回答流程中断）。</div>';
  } else {
    sourcesEl.innerHTML = '<div class="evidence-note">未检索到足够证据，无法从当前知识库作答。'
      + '请换个问法，或询问更贴近当前可检索知识（' + escapeHtml(RETRIEVAL_CONTEXT_LABEL)
      + '：部门制度与业务流程）的问题。</div>';
  }
  card.appendChild(sourcesEl);
  renderRelations(relations, card);

  (target || getChatTarget()).appendChild(card);
  scrollToBottom();
  return card;
}
export function renderAssistantCardFromHistory(msg, target) {
  const card = document.createElement('div');
  card.className = 'card assistant';
  const extra = msg.extra || {};
  const meta = extra.meta || {};
  const timings = meta.timings || {};
  const route = meta.route || ROUTE_FIXED;

  let html = '<div class="card-header">' + svgIcon('answer', 14) + '<span>回答</span></div>';
  html += metaStrip(timings, route);
  const modelStatus = meta.model_status || meta.model || {};
  const Aiunavilable = (typeof modelStatus.available === 'boolean' && modelStatus.available === false)
    || modelStatus.status === 'unavailable';
  if (Aiunavilable) {
    html += '<div class="warn-banner">' + svgIcon('failed', 14) + '<span>大模型当时不可用，该轮未生成回答，下方为已检索到的证据。</span></div>';
  }
  if (msg.content) html += '<div class="answer-body">' + escapeHtml(msg.content) + '</div>';
  else html += '<div class="answer-body empty">（无回答）</div>';
  card.innerHTML = html;

  const sourcesEl = document.createElement('div');
  sourcesEl.className = 'sources-section';
  const sources = msg.sources || [];
  if (sources.length) renderSources(sources, sourcesEl, { reranker: meta.reranker });
  else sourcesEl.innerHTML = '<div class="evidence-note">该轮未检索到来源证据。</div>';
  card.appendChild(sourcesEl);
  renderRelations(extra.relations || [], card);
  target.appendChild(card);
}
/* ═══════════════════════════════════════════════════════════════════
   知识工作台：业务模式选择器
   五个业务模式为「前端工作上下文」：非 Agent、非用户权限、非角色/密级/项目范围。
   非知识模式仅改变界面提示与会话标题前缀，绝不向 /api/paper-qa/ask 追加任何参数。
   ═══════════════════════════════════════════════════════════════════ */


export function bizMode() {
  return BUSINESS_MODES.find(m => m.id === state.currentBizMode)
    || BUSINESS_MODES.find(m => m.id === 'administration')
    || BUSINESS_MODES[BUSINESS_MODES.length - 1];
}
// 业务模式选择器按钮状态同步
export function syncBizSelector() {
  const mode = bizMode();
  document.querySelectorAll('#biz-selector-btns .biz-btn').forEach(b => {
    const active = b.dataset.mode === state.currentBizMode;
    b.classList.toggle('is-active', active);
    b.classList.toggle('connected', active && b.dataset.connected === '1');
    b.classList.toggle('pending', active && b.dataset.connected !== '1');
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });

  const notice = document.getElementById('biz-selector-notice');
  if (notice) {
    notice.hidden = false;
    notice.className = 'biz-selector-notice';
    notice.innerHTML =
      '<div class="notice-row"><span class="tag ok">当前检索上下文</span>'
      + '<span><b>' + escapeHtml(RETRIEVAL_CONTEXT_LABEL) + '</b>'
      + '（部门制度与业务流程的合成 / 整理语料，非真实公司数据）。</span></div>'
      + '<div class="notice-row"><span class="tag pending">' + escapeHtml(DEPT_FILTER_LABEL) + ' · 待接线</span>'
      + '<span><b>' + escapeHtml(mode.name) + '</b> 只切换助手视角与会话标题；'
      + escapeHtml(DEPT_FILTER_PENDING_NOTE) + '</span></div>';
  }

  const input = document.getElementById('task-input');
  if (input) {
    input.placeholder = mode.name + '助手 · 输入你的问题，例如：如何设计一场有效的绩效沟通？';
  }

  const hint = document.getElementById('biz-selector-hint');
  if (hint) hint.textContent = mode.name + '专业助手 · 知识库检索辅助';
}

export function selectBizMode(modeId) {
  const mode = BUSINESS_MODES.find(m => m.id === modeId);
  if (!mode) return;
  state.currentBizMode = modeId;
  syncBizSelector();
}

export function renderBizSelector() {
  const wrap = document.getElementById('biz-selector-btns');
  if (!wrap) return;

  wrap.innerHTML = BUSINESS_MODES.map(m =>
    '<button class="biz-btn" type="button"'
    + ' data-mode="' + m.id + '"'
    + ' data-connected="' + (m.connected ? '1' : '0') + '">'
    + '<span aria-hidden="true">' + bizIcon(m.icon, 15) + '</span>'
    + '<span>' + escapeHtml(m.name) + '</span>'
    + '</button>'
  ).join('');

  syncBizSelector();
}


/* 会话标题：非知识模式下加业务模式前缀，作为会话级前端上下文 */
export function contextTitle(question) {
  const topic = extractTopic(question);
  const mode = bizMode();
  return '[' + mode.name + '] ' + topic;
}
