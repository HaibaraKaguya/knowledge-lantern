/* 知题 · 本地学习记录 —— 示例记录 + 真实 OCR 接入
   示例记录为确定性本地样例（非真实学情）；「识别」页经 FastAPI 调用本机 LearningOCR。 */
'use strict';
const records = [
  { title: '配方法求二次函数最值', meta: '3 天前 · 做错', text: 'f(x)=-x²+4x+5，求最大值', tags: ['配方法', '最值'], score: 'BM25 命中“最大值” · 向量 0.92 · 图谱关联：前置方法' },
  { title: '顶点式与抛物线开口', meta: '10 天前 · 模糊', text: 'y=a(x-h)²+k 的顶点与最值', tags: ['顶点式', '图像'], score: 'BM25 命中“二次函数” · 向量 0.89 · 图谱关联：核心概念' },
  { title: '二次函数图像判断', meta: '18 天前 · 做错', text: '由开口方向判断最大值或最小值', tags: ['开口方向', '图像'], score: '向量 0.86 · 图谱：关联概念' },
  { title: '配方的符号错误', meta: '26 天前 · 不会', text: 'x²-6x 的配方法与常数项', tags: ['配方法', '粗心'], score: 'BM25 命中“配方” · 向量 0.83 · 图谱：前置方法' },
  { title: '函数最大值应用题', meta: '31 天前 · 做错', text: '围栏面积最大值的二次函数建模', tags: ['建模', '最值'], score: '向量 0.80 · 图谱：应用变式' },
];

function card(r, i, search) {
  const provenance = search
    ? '<div class="why"><button type="button" aria-expanded="false" data-why="' + i + '">为什么推荐？⌄</button>'
      + '<p class="why-detail" id="why-' + i + '" hidden>' + r.score + '。此记录作为独立图片证据返回。</p></div>'
    : '';
  return '<article class="record-card"><div class="thumb">' + r.text + '</div>'
    + '<div><h4>' + r.title + '</h4><p>' + r.meta + '</p>'
    + '<div class="chips">' + r.tags.map(t => '<span>' + t + '</span>').join('') + '</div></div>'
    + provenance + '</article>';
}
function renderLibrary() {
  document.querySelector('#library-cards').innerHTML = records.slice(0, 4).map((r, i) => card(r, i, false)).join('');
}
function renderSearch() {
  document.querySelector('#search-cards').innerHTML = records.map((r, i) => card(r, i, true)).join('');
}
function switchView(id) {
  document.querySelectorAll('.view,.nav').forEach(e => e.classList.remove('active'));
  document.querySelector('#' + id).classList.add('active');
  document.querySelectorAll('.nav').forEach(n => {
    const on = n.dataset.view === id;
    n.classList.toggle('active', on);
    if (on) n.setAttribute('aria-current', 'page');
    else n.removeAttribute('aria-current');
  });
}
document.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => switchView(b.dataset.view)));
document.querySelector('#search-form').addEventListener('submit', e => {
  e.preventDefault();
  document.querySelector('#search-results').hidden = false;
  renderSearch();
});
document.querySelector('#search-cards').addEventListener('click', e => {
  const b = e.target.closest('[data-why]');
  if (!b) return;
  const detail = document.querySelector('#why-' + b.dataset.why);
  const open = b.getAttribute('aria-expanded') === 'true';
  b.setAttribute('aria-expanded', String(!open));
  b.textContent = open ? '为什么推荐？⌄' : '为什么推荐？⌃';
  detail.hidden = open;
});
document.querySelectorAll('.filter').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach(x => {
    x.classList.remove('selected');
    x.setAttribute('aria-pressed', 'false');
  });
  b.classList.add('selected');
  b.setAttribute('aria-pressed', 'true');
}));
document.querySelectorAll('[data-toast]').forEach(b => b.addEventListener('click', () => {
  const t = document.querySelector('.toast');
  t.textContent = b.dataset.toast;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}));
renderLibrary();
renderSearch();

// ── OCR 识别接入（本机 LearningOCR；受控上传 + 主记录/审计分离）──────────
const ocrEsc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function ocrBadge(r) {
  return r.mock
    ? '<span class="ocr-badge mock">mock · 未加载真实模型</span>'
    : (r.real ? '<span class="ocr-badge real">real · 真实模型</span>' : '<span class="ocr-badge unknown">状态未知</span>');
}
function setOcrBusy(busy) {
  const btn = document.querySelector('#ocr-submit');
  const spinner = document.querySelector('#ocr-spinner');
  const text = document.querySelector('#ocr-submit-text');
  if (btn) { btn.disabled = !!busy; btn.setAttribute('aria-busy', busy ? 'true' : 'false'); }
  if (spinner) spinner.classList.toggle('active', !!busy);
  if (text) text.textContent = busy ? '识别中…' : '开始识别';
}
function renderOcrResult(r) {
  const box = document.querySelector('#ocr-result');
  box.hidden = false;
  const rec = r.record || {}, q = rec.question || {};
  const warnings = (r.validation && r.validation.warnings) || [];
  const errors = (r.validation && r.validation.errors) || [];
  if (r.status === 'failed') {
    box.innerHTML = '<div class="ocr-card"><div class="section-row"><h3>识别失败</h3>' + ocrBadge(r) + '</div>'
      + '<p class="muted">' + ocrEsc(r.error || '未知错误') + '</p>'
      + '<small>失败不会伪造结果：未产出任何结构化记录。</small></div>';
    return;
  }
  box.innerHTML = '<div class="ocr-card"><div class="section-row"><h3>' + ocrEsc(rec.id || r.record_id) + '</h3>'
    + ocrBadge(r) + (r.reused ? '<span class="ocr-badge reused">重复上传 · 复用既有记录</span>' : '') + '</div>'
    + '<p class="muted">模式 ' + ocrEsc(r.mode_requested) + ' → ' + ocrEsc(r.mode_effective)
    + ' · OCR ' + ocrEsc(r.ocr_status) + ' · 模型 ' + ocrEsc(r.model_status)
    + ' · 校验 ' + (r.validation && r.validation.valid ? '通过' : '未通过')
    + ' · 文件 ' + ocrEsc(r.image_filename || '—') + '</p>'
    + '<p>' + ocrEsc(q.text || '') + '</p>'
    + '<div class="chips">' + (rec.knowledge_points || []).map(k => '<span>' + ocrEsc(k) + '</span>').join('') + '</div>'
    + ((q.formulas || []).length ? '<h4>公式</h4><ul>' + q.formulas.map(f => '<li><code>' + ocrEsc(f.latex) + '</code> — ' + ocrEsc(f.meaning) + '</li>').join('') + '</ul>' : '')
    + ((q.requirements || []).length ? '<h4>要求</h4><ul>' + q.requirements.map(x => '<li>' + ocrEsc(x) + '</li>').join('') + '</ul>' : '')
    + (rec.user_note && rec.user_note.text ? '<h4>备注</h4><p>' + ocrEsc(rec.user_note.text) + (rec.user_note.knowledge_gap ? '（推断缺口：' + ocrEsc(rec.user_note.knowledge_gap) + '）' : '') + '</p>' : '')
    + '<p class="rank-note">检索用文本：' + ocrEsc(rec.retrieval_text || '') + '</p>'
    + (warnings.length ? '<details><summary>校验警告 ' + warnings.length + ' 条</summary><ul>' + warnings.map(w => '<li>' + ocrEsc(w) + '</li>').join('') + '</ul></details>' : '')
    + (errors.length ? '<details open><summary>校验错误 ' + errors.length + ' 条</summary><ul>' + errors.map(x => '<li>' + ocrEsc(x) + '</li>').join('') + '</ul></details>' : '')
    + '<small>主记录与审计 sidecar 分开保存；审计 sidecar 含 OCR 证据、模型/mock 状态与清理日志。</small></div>';
}
async function loadOcrHealth() {
  const el = document.querySelector('#ocr-health');
  if (!el) return;
  try {
    const res = await fetch('/api/study/ocr/health');
    const b = await res.json();
    const lo = b.learningocr || {};
    el.textContent = '能力状态：' + b.status + ' · real ' + (lo.real_available ? '可用' : '不可用')
      + ' · mock 可用' + (lo.error ? ' · ' + lo.error : '');
    el.dataset.state = lo.real_available ? 'ok' : (lo.error ? 'warn' : 'warn');
  } catch (err) {
    el.textContent = '能力状态：无法连接服务（请经 FastAPI :8000 打开本页）';
    el.dataset.state = 'error';
  }
}
document.querySelector('#ocr-form').addEventListener('submit', async e => {
  e.preventDefault();
  const statusEl = document.querySelector('#ocr-status');
  const file = document.querySelector('#ocr-file').files[0];
  const typed = document.querySelector('#ocr-image').value.trim();
  const mode = document.querySelector('#ocr-mode').value;
  const note = document.querySelector('#ocr-note').value.trim();
  setOcrBusy(true);
  try {
    let reference = typed;
    if (file) {
      statusEl.textContent = '上传到受控目录…';
      const up = await fetch('/api/study/ocr/uploads?filename=' + encodeURIComponent(file.name), { method: 'POST', body: file });
      const ub = await up.json();
      if (!up.ok) throw new Error((ub.detail && ub.detail.detail) || '上传失败');
      reference = ub.reference;
    }
    if (!reference) throw new Error('请选择图片或填写受控图片引用');
    statusEl.textContent = '识别中…（real 模式首次加载可能较慢）';
    const res = await fetch('/api/study/ocr/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_reference: reference, mode: mode, user_note: note || null }),
    });
    const body = await res.json();
    if (!res.ok) {
      const d = body.detail;
      throw new Error((d && (d.detail || d.error)) || '请求被拒绝');
    }
    statusEl.textContent = '状态：' + body.status + ' · ' + body.mode_effective
      + (body.mock ? ' · mock' : '') + (body.reused ? ' · 复用既有记录' : '');
    renderOcrResult(body);
  } catch (err) {
    statusEl.textContent = '失败：' + err.message;
  } finally {
    setOcrBusy(false);
  }
});
loadOcrHealth();
