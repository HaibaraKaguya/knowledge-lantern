/* API 模块：后续从 app.js 拆分。 */
'use strict';
import { activeApiBase, FALLBACK_API_BASE, setActiveApiBase } from './config.js';
import { updateEndpointChip, showToast } from './ui.js';
/* ═══════════════════════════════════════════════════════════════════
   API Helper — 自动 fallback；silent 用于健康轮询（不弹 toast）
   ═══════════════════════════════════════════════════════════════════ */
 export async function apiFetch(path, options = {}) {
  const { silent, ...fetchOptions } = options;
  try {
    return await fetch(activeApiBase + path, fetchOptions);
  } catch (e) {
    if (e.name === 'AbortError') throw e;   // 用户主动取消：不弹错误、不切换端点
    if (activeApiBase !== FALLBACK_API_BASE && e instanceof TypeError) {
      console.warn('API %s unreachable, switching to fallback %s', activeApiBase, FALLBACK_API_BASE);
      setActiveApiBase(FALLBACK_API_BASE);
      updateEndpointChip();
      try {
        return await fetch(activeApiBase + path, fetchOptions);
      } catch (e2) {
        if (!silent) showToast('无法连接后端服务，请确认 server 是否已启动（python -m src.server）', 'error');
        throw e2;
      }
    }
    if (!silent) showToast('网络错误：无法连接 ' + activeApiBase + path, 'error');
    throw e;
  }
}
