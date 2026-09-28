/* 应用运行时状态 */
'use strict';

export const state = {
  sessions: [],
  currentSessionId: null,
  isRunning: false,
  abortController: null,
  lastQuestion: null,
  lastRetryCard: null,
  healthTimer: null,
  lastHealthData: null,
  currentView: 'home',
  currentChartType: 'line',
  chartHoverMeta: null,
  currentBizMode: 'administration',
  currentUserId: 'employee'
};
