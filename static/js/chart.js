/* 图表模块：后续从 app.js 拆分。 */
'use strict';
import { BUSINESS_MODES, GRAN_LABELS } from './config.js';
import { state } from './state.js';
import { escapeHtml } from './ui.js';
/* ═══════════════════════════════════════════════════════════════════
   原生 SVG 图表
   ═══════════════════════════════════════════════════════════════════ */
function fmtValue(v, decimals, thousands) {
  if (v == null) return '—';
  if (thousands) return Number(v).toLocaleString('zh-CN');
  return decimals > 0 ? v.toFixed(decimals) : String(Math.round(v));
}

function niceTicks(min, max, count) {
  if (!isFinite(min) || !isFinite(max)) return [0];
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / Math.max(count - 1, 1);
  if (step0 <= 0) return [0];
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  let step;
  if (norm >= 5) step = 5 * mag;
  else if (norm >= 2) step = 2 * mag;
  else if (norm >= 1) step = mag;
  else step = 0.5 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 0.5; v += step) {
    const r = Math.round(v / step) * step;
    const clean = Math.abs(r) < step * 1e-6 ? 0 : r;
    ticks.push(clean);
    if (ticks.length > 30) break;
  }
  return ticks;
}

function fmtTick(v, decimals) {
  if (decimals > 0) return v.toFixed(decimals);
  return String(Math.round(v));
}

const CHART_INK = {
  series: '#3987e5',          // 深色分类第 1 槽（蓝）
  seriesAlt: 'rgba(90,162,255,0.85)',
  grid: 'rgba(232,238,247,0.08)',
  axis: '#33415e',
  label: '#8493ad',
  value: '#a8b6cc',
  cross: 'rgba(168,182,204,0.5)',
};

export function renderChart() {
  const modeSel = document.getElementById('chart-mode');
  const granSel = document.getElementById('chart-granularity');
  if (!modeSel) return;
  const modeId = modeSel.value;
  const mode = BUSINESS_MODES.find(m => m.id === modeId) || BUSINESS_MODES[0];
  const gran = granSel ? granSel.value : 'month';
  const type = state.currentChartType;
  const isReal = !!mode.connected;

  const data = isReal ? knowledgeSeries() : genDemoSeries(mode, gran);
  const svgEl = document.getElementById('chart-svg');
  const badge = document.getElementById('chart-data-badge');
  const title = document.getElementById('chart-main-title');
  const statusEl = document.getElementById('chart-status');
  const rangeEl = document.getElementById('chart-range');
  const sourceEl = document.getElementById('chart-source');
  const tableBody = document.getElementById('chart-table-body');

  if (badge) {
    badge.textContent = isReal ? '知识库实时计数' : '示例数据 · 前端生成';
    badge.className = 'data-badge ' + (isReal ? 'real' : 'demo');
  }
  if (title) title.textContent = isReal
    ? mode.name + ' · 知识库表计数（当前快照）'
    : mode.name + ' · ' + (GRAN_LABELS[gran] || '') + '示例趋势';
  if (statusEl) statusEl.textContent = isReal
    ? '真实计数，来自知识库 health 快照'
    : '前端生成的示例序列，非真实经营数据';
  const last = data.labels[data.labels.length - 1];
  if (rangeEl) rangeEl.textContent = isReal
    ? '当前快照（非时间序列）'
    : (data.labels[0] + ' ~ ' + last);
  if (sourceEl) sourceEl.innerHTML = isReal
    ? '<code>GET /api/paper-qa/health</code> · knowledge_db.counts'
    : '前端按所选业务模式生成，仅用于界面示例；检索上下文为测试期种子知识，不代表真实业务';

  const meta = {
    type,
    labels: data.labels,
    values: data.values,
    unit: data.unit,
    decimals: mode.decimals,
    thousands: isReal,
    titleAttr: title ? title.textContent : (mode.name + ' 分析图'),
  };
  if (svgEl) {
    svgEl.innerHTML = buildSVG(meta);
    bindChartHover(svgEl, meta);
  }
  drawChartTable(tableBody, meta);
}

function buildSVG(meta) {
  const { type, labels, values, unit, decimals, thousands } = meta;
  const W = 760, H = 300;
  const M = { top: 18, right: 18, bottom: 46, left: 58 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const n = labels.length;
  const nums = values.filter(v => v != null);

  let tMin, tMax;
  if (type === 'bar') {
    tMin = 0;
    tMax = Math.max.apply(null, nums.concat([1]));
  } else {
    const lo = Math.min.apply(null, nums.concat([0]));
    const hi = Math.max.apply(null, nums.concat([1]));
    const pad = (hi - lo) * 0.15 || 1;
    tMin = lo - pad;
    tMax = hi + pad;
  }
  const ticks = niceTicks(tMin, tMax, 5);
  if (ticks.length >= 2) { tMin = ticks[0]; tMax = ticks[ticks.length - 1]; }
  const scale = (tMax - tMin) || 1;
  const y = v => M.top + plotH - ((v - tMin) / scale) * plotH;

  const xs = [];
  let i;
  for (i = 0; i < n; i++) {
    xs.push(type === 'bar'
      ? M.left + plotW * ((i + 0.5) / n)
      : (n === 1 ? M.left + plotW / 2 : M.left + plotW * (i / (n - 1))));
  }

  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img"'
    + ' aria-label="' + escapeHtml(meta.titleAttr || '') + '">';

  // 选择性直接标注：点少全部标注，点多只标最大 / 最小值
  let maxIdx = 0, minIdx = 0, hasAny = false;
  values.forEach((v, idx) => {
    if (v == null) return;
    if (!hasAny) { hasAny = true; maxIdx = minIdx = idx; }
    else {
      if (v > values[maxIdx]) maxIdx = idx;
      if (v < values[minIdx]) minIdx = idx;
    }
  });
  const shouldLabel = idx => n <= 8 || idx === maxIdx || idx === minIdx;

  // 网格线 + 轴线
  ticks.forEach(t => {
    const yy = y(t);
    if (Math.abs(t - tMin) < 1e-9) {
      svg += '<line x1="' + M.left + '" y1="' + yy + '" x2="' + (M.left + plotW) + '" y2="' + yy
        + '" stroke="' + CHART_INK.axis + '" stroke-width="1" />';
    } else {
      svg += '<line x1="' + M.left + '" y1="' + yy + '" x2="' + (M.left + plotW) + '" y2="' + yy
        + '" stroke="' + CHART_INK.grid + '" stroke-width="1" />';
    }
    svg += '<text x="' + (M.left - 10) + '" y="' + (yy + 4) + '" text-anchor="end"'
      + ' font-size="11" fill="' + CHART_INK.label + '" font-family="var(--font-mono, monospace)">'
      + escapeHtml(fmtTick(t, decimals)) + '</text>';
  });

  // X 轴标签（点过多时隔项显示）
  const stepX = n >= 11 ? 2 : 1;
  for (i = 0; i < n; i++) {
    if (i % stepX !== 0 && i !== n - 1) continue;
    svg += '<text x="' + xs[i] + '" y="' + (H - M.bottom + 20) + '" text-anchor="middle"'
      + ' font-size="11" fill="' + CHART_INK.label + '">' + escapeHtml(labels[i]) + '</text>';
  }

  // 数据标记
  if (type === 'bar') {
    const band = plotW / n;
    const barW = Math.max(6, Math.min(band * 0.6, 42));
    values.forEach((v, idx) => {
      const x0 = xs[idx] - barW / 2;
      const y0 = y(v == null ? 0 : v);
      const h = Math.max(y(0) - y0, 0);
      svg += '<rect class="c-bar" data-idx="' + idx + '" x="' + x0 + '" y="' + y0 + '" width="' + barW
        + '" height="' + h + '" rx="3" fill="' + CHART_INK.series + '" opacity="0.92" />';
      if (shouldLabel(idx)) drawValueLabel(svg, v, xs[idx], y0, decimals, thousands, M.top, 'below-if-top');
    });
  } else {
    const points = values.map((v, idx) => {
      if (v == null) return null;
      const yy = y(v);
      svg += '<line class="c-line-seg" x1="' + xs[idx] + '" y1="' + yy + '" x2="' + xs[idx] + '" y2="' + (M.top + plotH)
        + '" stroke="' + CHART_INK.grid + '" stroke-width="1" stroke-dasharray="3 4" opacity="0.6" />';
      return xs[idx] + ',' + yy;
    }).filter(Boolean);
    if (points.length > 1) {
      svg += '<polyline points="' + points.join(' ') + '" fill="none" stroke="' + CHART_INK.series
        + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />';
    }
    // 面积填充
    if (points.length > 1 && values[0] != null) {
      svg += '<polyline points="' + points.join(' ')
        + ' ' + xs[n - 1] + ',' + y(0) + ' ' + xs[0] + ',' + y(0)
        + '" fill="' + CHART_INK.seriesAlt + '" opacity="0.10" stroke="none" />';
    }
    // 数据点
    values.forEach((v, idx) => {
      if (v == null) return;
      const yy = y(v);
      const r = n <= 12 ? 3.5 : 0;
      if (r) svg += '<circle class="c-pt" data-idx="' + idx + '" cx="' + xs[idx] + '" cy="' + yy
        + '" r="' + r + '" fill="' + CHART_INK.series + '" />';
      if (shouldLabel(idx)) drawValueLabel(svg, v, xs[idx], yy, decimals, thousands, M.top, 'below-if-top');
    });
  }

  // 十字线 + 悬停点（hover 时显示）
  svg += '<line id="chart-cross" x1="0" y1="0" x2="0" y2="0" stroke="' + CHART_INK.cross
    + '" stroke-width="1" opacity="0" />';
  if (type === 'line') {
    svg += '<circle id="chart-dot" cx="0" cy="0" r="4.5" fill="' + CHART_INK.series
      + '" stroke="var(--bg-card, #151e31)" stroke-width="2" opacity="0" />';
  }

  svg += '</svg>';

  state.chartHoverMeta = {
    W, H, M, y, xs, values, labels, unit, decimals, thousands, type, tMin,
  };
  return svg;
}

function drawValueLabel(svg, v, x, yy, decimals, thousands, topPad, mode) {
  if (v == null) return;
  let labelY = yy - 8;
  if (mode === 'below-if-top' && yy < topPad + 14) labelY = yy + 16;
  svg += '<text x="' + x + '" y="' + labelY + '" text-anchor="middle"'
    + ' font-size="10.5" fill="' + CHART_INK.value + '" font-weight="600">'
    + escapeHtml(fmtValue(v, decimals, thousands)) + '</text>';
}

function ensureTooltip(container) {
  let tip = container.querySelector('.chart-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'chart-tooltip';
    tip.innerHTML = '<div class="tt-title"></div><div class="tt-val"></div>';
    container.appendChild(tip);
  }
  return tip;
}

function bindChartHover(container, meta) {
  const svg = container.querySelector('svg');
  if (!svg || !state.chartHoverMeta) return;
  const hm = state.chartHoverMeta;
  const tooltip = ensureTooltip(container);
  const cross = svg.querySelector('#chart-cross');
  const dot = svg.querySelector('#chart-dot');
  const bars = Array.prototype.slice.call(svg.querySelectorAll('.c-bar'));

  function nearest(px) {
    let idx = 0, best = Infinity;
    hm.xs.forEach((x, i) => {
      const d = Math.abs(x - px);
      if (d < best) { best = d; idx = i; }
    });
    return idx;
  }

  container.addEventListener('mousemove', (e) => {
    const r = svg.getBoundingClientRect();
    if (!r.width) return;
    const xSvg = (e.clientX - r.left) / r.width * hm.W;
    const i = nearest(xSvg);
    const cx = hm.xs[i];
    if (cross) { cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('opacity', '1'); }
    if (dot && hm.values[i] != null) {
      dot.setAttribute('cx', cx);
      dot.setAttribute('cy', hm.y(hm.values[i]));
      dot.setAttribute('opacity', '1');
    }
    bars.forEach((b, bi) => b.setAttribute('opacity', bi === i ? '1' : '0.45'));

    const val = hm.values[i];
    const tx = (cx / hm.W) * r.width;
    const ty = (val != null ? hm.y(val) : hm.y(hm.tMin)) / hm.H * r.height;
    tooltip.querySelector('.tt-title').textContent = hm.labels[i];
    tooltip.querySelector('.tt-val').textContent = hm.unit + ' · '
      + (val == null ? '缺失' : fmtValue(val, hm.decimals, hm.thousands));
    tooltip.style.left = Math.max(48, Math.min(r.width - 48, tx)) + 'px';
    tooltip.style.top = Math.max(0, ty - 8) + 'px';
    tooltip.style.transform = ty < 70
      ? 'translate(-50%, 14px)' : 'translate(-50%, calc(-100% - 12px))';
    tooltip.classList.add('show');
  });
  container.addEventListener('mouseleave', () => {
    if (cross) cross.setAttribute('opacity', '0');
    if (dot) dot.setAttribute('opacity', '0');
    bars.forEach(b => b.setAttribute('opacity', '1'));
    tooltip.classList.remove('show');
  });
}

function drawChartTable(body, meta) {
  if (!body) return;
  body.innerHTML = '<table>'
    + '<thead><tr><th>类别 / 时间</th><th>数值</th></tr></thead><tbody>'
    + meta.labels.map((l, i) => {
      const v = meta.values[i];
      const vs = v == null ? '—' : fmtValue(v, meta.decimals, meta.thousands);
      return '<tr><td>' + escapeHtml(l) + '</td><td>' + escapeHtml(vs) + '</td></tr>';
    }).join('')
    + '</tbody></table>';
}

/* ═══════════════════════════════════════════════════════════════════
   图表控制
   ═══════════════════════════════════════════════════════════════════ */
export function populateChartMode() {
  const sel = document.getElementById('chart-mode');
  if (!sel) return;
  sel.innerHTML = BUSINESS_MODES.map(m =>
    '<option value="' + m.id + '">' + escapeHtml(m.name) + '</option>').join('');
}

export function onChartModeChange() {
  const modeId = document.getElementById('chart-mode').value;
  const mode = BUSINESS_MODES.find(m => m.id === modeId);
  const isReal = !!(mode && mode.connected);
  const granSel = document.getElementById('chart-granularity');
  const granCtl = document.getElementById('ctl-granularity');
  const segBtns = Array.prototype.slice.call(document.querySelectorAll('#chart-type-seg .seg-btn'));
  if (granSel) granSel.disabled = isReal;
  if (granCtl) granCtl.style.opacity = isReal ? '0.5' : '';
  segBtns.forEach(b => {
    const isLine = b.dataset.type === 'line';
    b.disabled = isReal && isLine;
    const active = isReal ? b.dataset.type === 'bar' : b.dataset.type === state.currentChartType;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  if (isReal) state.currentChartType = 'bar';
}

function pad2(n) { return String(n).padStart(2, '0'); }
function fmtDate(d) { return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }
function granLabels(gran) {
  const now = new Date();
  if (gran === 'day') return Array.from({ length: 14 }, (_, i) => fmtDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 13 + i)));
  if (gran === 'week') return Array.from({ length: 8 }, (_, i) => fmtDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() - (7 - i) * 7)));
  if (gran === 'month') return Array.from({ length: 12 }, (_, i) => (i + 1) + '月');
  return ['Q1', 'Q2', 'Q3', 'Q4'];
}
function hashSeed(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function mulberry32(a) { return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
function genDemoSeries(mode, gran) {
  const n = gran === 'day' ? 14 : gran === 'week' ? 8 : gran === 'month' ? 12 : 4;
  const rand = mulberry32(hashSeed(mode.id + ':' + gran)); const labels = granLabels(gran); const values = []; let v = mode.base * (0.9 + rand() * 0.1);
  for (let i = 0; i < n; i++) { const drift = ((i / Math.max(n - 1, 1)) - 0.5) * mode.amp * 0.6; const noise = (rand() - 0.5) * mode.amp; v = Math.max(mode.base * 0.55, Math.min(mode.base * 1.45, v + drift + noise * 0.4)); values.push(Number(v.toFixed(mode.decimals))); }
  return { labels, values, unit: mode.metric };
}
function knowledgeSeries() { return { labels: [], values: [], unit: '计数（条）' }; }
