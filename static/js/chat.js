/* 聊天模块：后续从 app.js 拆分。 */
'use strict';
import { BUSINESS_MODES, LS_KEY, MAX_SESSIONS, ROUTE_FIXED } from './config.js';
import { state } from './state.js';
import { apiFetch } from './api.js';
import { showToast, setStatus, getChatTarget, scrollToBottom, escapeHtml, renderAssistantCard, renderAssistantCardFromHistory, contextTitle, renderEmptyState, svgIcon } from './ui.js';
export function setCrumbCurrent(text) {
  const el = document.getElementById('crumb-current');
  if (el) el.textContent = text || '知识问答工作空间';
}
/* ═══════════════════════════════════════════════════════════════════
   提交 / 取消 / 重试
   ═══════════════════════════════════════════════════════════════════ */
export function updateRunState() {
  const btn = document.getElementById('submit-btn');
  const spinner = document.getElementById('spinner');
  const text = document.getElementById('submit-text');
  const cancelBtn = document.getElementById('cancel-btn');
  const input = document.getElementById('task-input');
  if (btn) {
    btn.disabled = state.isRunning;
    btn.setAttribute('aria-busy', state.isRunning ? 'true' : 'false');
  }
  if (spinner) spinner.classList.toggle('active', state.isRunning);
  if (text) text.textContent = state.isRunning ? '检索中' : '发送';
  if (cancelBtn) cancelBtn.style.display = state.isRunning ? 'inline-flex' : 'none';
  if (input) {
    input.disabled = state.isRunning;
    input.setAttribute('aria-disabled', state.isRunning ? 'true' : 'false');
  }
}

export function submitQuestion() {
  const input = document.getElementById('task-input');
  if (!input) {
    showToast('找不到问题输入框，请刷新页面后重试', 'error');
    return;
  }
  const text = input.value.trim();
  if (state.isRunning) {
    showToast('正在检索中，请稍候或点击「取消」', 'warn');
    return;
  }
  if (!text) {
    showToast('请输入问题后再发送', 'warn');
    input.focus && input.focus();
    return;
  }
  state.lastQuestion = text;
  input.value = '';
  askQuestion(text, {});
}

export function cancelRequest() {
  if (state.abortController) state.abortController.abort();
}

export function retryLastQuestion() {
  if (!state.lastQuestion || state.isRunning) return;
  if (state.lastRetryCard && state.lastRetryCard.parentNode) {
    state.lastRetryCard.parentNode.removeChild(state.lastRetryCard);
  }
  state.lastRetryCard = null;
  askQuestion(state.lastQuestion, { skipUserCard: true });
}

export async function askQuestion(question, opts) {
  state.isRunning = true;
  state.abortController = new AbortController();
  updateRunState();
  setStatus('running', '检索中…');

  const target = getChatTarget();
  if (!opts.skipUserCard) {
    setCrumbCurrent(contextTitle(question));
    const userCard = document.createElement('div');
    userCard.className = 'card user';
    userCard.innerHTML = '<div class="card-header">' + svgIcon('question', 14) + '<span>问题</span></div><div>' + escapeHtml(question) + '</div>';
    target.appendChild(userCard);
  }

  const pending = document.createElement('div');
  pending.className = 'card assistant pending';
  pending.innerHTML = '<div class="card-header">' + svgIcon('answer', 14) + '<span>回答</span></div>'
    + '<div class="pending-row"><span class="spinner active"></span>'
    + '<span>正在按权限检索部门知识库并生成回答…</span></div>';
  target.appendChild(pending);
  scrollToBottom();

  try {
    const res = await apiFetch('/api/paper-qa/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: state.abortController.signal,
      body: JSON.stringify({ question: buildAgentQuestion(question), session_id: ensureSession(), user_id: state.currentUserId }),
    });

    if (!res.ok) {
      let errMsg = 'HTTP ' + res.status + ' ' + res.statusText;
      let body = null;
      try { body = await res.json(); } catch (_) { /* 保留默认信息 */ }
      if (body && typeof body === 'object'
          && (body.model_status !== undefined || body.model !== undefined || body.sources !== undefined)) {
        // 后端在非 200 响应中仍返回了结构化结果（例如模型服务不可用时仍携带检索证据）
        pending.remove();
        renderAssistantCard({
          answer: body.answer || '',
          route: body.route || ROUTE_FIXED,
          model_status: body.model_status || body.model || { status: 'unavailable', error: errMsg },
          sources: body.sources || [],
          timings: body.timings || {},
          database_counts: body.database_counts || body.database || {},
        }, target);
        state.lastRetryCard = null;
        persistTurn(body, question);
        refreshSessions();
        setStatus('idle', '完成');
      } else {
        errMsg = (body && (body.detail || body.error || body.message)) || errMsg;
        throw new Error(errMsg);
      }
    } else {
      const data = await res.json();
      pending.remove();
      renderAssistantCard(data, target);
      state.lastRetryCard = null;
      persistTurn(data, question);
      refreshSessions();
      setStatus('idle', '完成');
    }
  } catch (e) {
    console.error('askQuestion failed:', e);
    pending.remove();
    if (e.name === 'AbortError') {
      const card = document.createElement('div');
      card.className = 'card assistant';
      card.innerHTML = '<div class="card-header">' + svgIcon('cancel', 14) + '<span>已取消</span></div>'
        + '<div class="answer-body empty">本次请求已取消。</div>'
        + '<div class="retry-row"><button class="btn btn-ghost btn-sm retry-btn" type="button">' + svgIcon('refresh', 14) + '重试</button></div>';
      target.appendChild(card);
      state.lastRetryCard = card;
      setStatus('idle', '已取消');
    } else {
      const card = document.createElement('div');
      card.className = 'card assistant';
      card.innerHTML = '<div class="card-header">' + svgIcon('failed', 14) + '<span>请求失败</span></div>'
        + '<div class="answer-body empty">' + escapeHtml(e.message || '未知错误') + '</div>'
        + '<div class="retry-row"><button class="btn btn-ghost btn-sm retry-btn" type="button">' + svgIcon('refresh', 14) + '重试</button></div>';
      target.appendChild(card);
      state.lastRetryCard = card;
      setStatus('error', '失败');
      showToast('请求失败：' + (e.message || ''), 'error');
    }
  } finally {
    state.isRunning = false;
    state.abortController = null;
    updateRunState();
  }
}
function buildAgentQuestion(question) {
  const mode = BUSINESS_MODES.find(item => item.id === state.currentBizMode) || BUSINESS_MODES[0];
  return '[专业助手上下文]\n' + (mode.agentInstruction || '')
    + '\n当前业务领域：' + mode.name
    + '\n请优先依据检索证据回答；若证据不足，明确说明并给出通用建议。\n\n[用户问题]\n' + question;
}
export function persistTurn(data, question) {
  const sessionId = data.session_id || state.currentSessionId;
  state.currentSessionId = sessionId;
  let s = state.sessions.find(x => x.id === sessionId);
  if (!s) {
    s = { id: sessionId, title: contextTitle(question), createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
    state.sessions.push(s);
  }
  const userMsg = { role: 'user', content: question, ts: Date.now() };
  const assistantMsg = {
    role: 'assistant',
    content: data.answer || '',
    sources: data.sources || [],
    extra: {
      relations: data.relations || [],
      meta: {
        route: data.route,
        timings: data.timings,
        model_status: data.model_status || data.model,
        database_counts: data.database_counts || data.database,
        reranker: data.reranker || null,
      },
    },
    ts: Date.now(),
  };
  s.messages.push(userMsg, assistantMsg);
  const userCount = s.messages.filter(m => m.role === 'user').length;
  if (userCount === 1) s.title = contextTitle(question);
  s.updatedAt = Date.now();
  saveSessions();
}
function ensureSession() {
  if (!state.currentSessionId) {
    const now = new Date();
    const pad = n => String(n).padStart(2, '0');
    state.currentSessionId = 'paper-' + now.getFullYear()
      + pad(now.getMonth() + 1) + pad(now.getDate()) + '-'
      + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
  }
  return state.currentSessionId;
}
export function saveSessions() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(state.sessions.slice(0, MAX_SESSIONS)));
  } catch (e) {
    console.warn('localStorage 保存失败:', e);
  }
}
export function loadSessions() {
  try {
    state.sessions = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
    if (!Array.isArray(state.sessions)) state.sessions = [];
  } catch (e) {
    state.sessions = [];
  }
}
export function refreshSessions() {
  renderSessions();
}

export function renderSessions() {
  const list = document.getElementById('session-list');
  if (!list) return;
  const sorted = state.sessions.slice().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  if (!sorted.length) {
    list.innerHTML = '<div class="empty-list">暂无本页会话</div>';
    return;
  }
  list.innerHTML = sorted.map(s => {
    const rounds = s.messages ? Math.ceil(s.messages.filter(m => m.role === 'user').length) : 0;
    return '<div class="session-item ' + (s.id === state.currentSessionId ? 'active' : '') + '" data-session-id="'
      + escapeHtml(s.id) + '">'
      + '<span class="icon" aria-hidden="true">' + svgIcon('chat', 14) + '</span>'
      + '<span class="title">' + escapeHtml(s.title || '未命名会话') + '</span>'
      + '<span class="steps">' + rounds + '</span>'
      + '</div>';
  }).join('');
  applySessionFilter();
}

export function applySessionFilter() {
  const q = ((document.getElementById('session-search') || {}).value || '').trim().toLowerCase();
  const list = document.getElementById('session-list');
  if (!list) return;
  list.querySelectorAll('.session-item').forEach(el => {
    const title = ((el.querySelector('.title') || {}).textContent || '').toLowerCase();
    const id = (el.dataset.sessionId || '').toLowerCase();
    el.style.display = (title.includes(q) || id.includes(q)) ? '' : 'none';
  });
}

export function selectSession(id) {
  if (state.isRunning) { showToast('请等待当前检索完成', 'warn'); return; }
  const s = state.sessions.find(x => x.id === id);
  if (!s) return;
  state.currentSessionId = id;
  setCrumbCurrent(s.title || '会话');
  setStatus('idle', '就绪');
  renderMessages(s.messages || []);
  renderSessions();
}

export function renderMessages(messages) {
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  const inner = document.createElement('div');
  inner.id = 'chat-inner';
  inner.className = 'chat-inner';
  chat.appendChild(inner);
  const target = inner;
  if (!messages.length) {
    target.innerHTML = '<div class="empty-state"><div class="logo-ring" aria-hidden="true">' + svgIcon('answer', 30) + '</div>'
      + '<h3>暂无对话记录</h3><p class="tagline">该会话没有可回放的消息</p></div>';
    return;
  }
  messages.forEach(msg => {
    if (msg.role === 'user') {
      const c = document.createElement('div');
      c.className = 'card user';
      c.innerHTML = '<div class="card-header">' + svgIcon('question', 14) + '<span>问题</span></div><div>' + escapeHtml(msg.content) + '</div>';
      target.appendChild(c);
    } else {
      renderAssistantCardFromHistory(msg, target);
    }
  });
  scrollToBottom();
}

export function newSession() {
  if (state.isRunning) { showToast('请等待当前检索完成', 'warn'); return; }
  state.currentSessionId = null;
  const chat = document.getElementById('chat-area');
  chat.innerHTML = '';
  renderEmptyState(chat);
  setStatus('idle', '就绪');
  setCrumbCurrent('知识问答工作空间');
  renderSessions();
}
