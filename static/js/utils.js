'use strict';
export function fmtDuration(ms) {
  const n = Number(ms);
  if (Number.isNaN(n)) return String(ms == null ? '' : ms);
  if (n < 1000) return Math.round(n) + 'ms';
  return (n / 1000).toFixed(1) + 's';
}
export function formatScore(score) {
  const n = Number(score);
  if (score == null || Number.isNaN(n)) return '—';
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2);
}
//整理後端出來的數據
export function extractCounts(data) {
  const pqa = data && data.paper_qa;
  const kdb = pqa && pqa.knowledge_db ? pqa.knowledge_db.counts : null;
  const cp = pqa && pqa.checkpoints_db ? pqa.checkpoints_db : null;
  const audit = pqa && pqa.audit_db ? pqa.audit_db : null;
  const model = pqa && pqa.model ? pqa.model : null;
  return { kdb, turns: cp ? cp.turns : null, events: audit ? audit.events : null, model };
}
//格式化數字輸出
export function fmtCount(v) {
  return (v == null || v === -1) ? '—' : Number(v).toLocaleString('zh-CN');
}
/* ═══════════════════════════════════════════════════════════════════
   来源归一化 
   ═══════════════════════════════════════════════════════════════════ */
export function normalizeSource(src, index) {
  if (typeof src === 'string') {
    return { rank: index + 1, title: src, heading: '', chunk_id: '', score: null, text: src, page: '', match_type: '' };
  }
  if (!src || typeof src !== 'object') return null;
  const rank = src.rank != null ? src.rank
    : src.source != null ? Number(src.source)
    : src.position != null ? (Number(src.position) + 1)
    : index + 1;
  const title = src.title || src.paper_title || src.document_title || src.name || src.filename || '未命名文档';
  const heading = src.heading || src.heading_path || src.section || src.chapter || '';
  const chunk_id = src.chunk_id || src.chunkId || src.id || '';
  const score = src.score != null ? src.score
    : src.retrieval_score != null ? src.retrieval_score
    : src.bm25 != null ? src.bm25
    : null;
  const text = src.text || src.excerpt || src.snippet || src.evidence || src.content || '';
  const page = src.page != null ? src.page : (src.page_no != null ? src.page_no : '');
  const match_type = src.match_type || src.matched || '';
  const branch_ranks = src.branch_ranks || {};
  const branch_contributions = src.branch_contributions || {};
  const rrf_rank = src.rrf_rank != null ? src.rrf_rank : null;
  const reranker_score = src.reranker_score != null ? src.reranker_score : null;
  const final_rank = src.final_rank != null ? src.final_rank : null;
  return { rank, title, heading, chunk_id, score, text, page, match_type,
           branch_ranks, branch_contributions, rrf_rank, reranker_score, final_rank };
}
