// Settings → «Синхронизация». Two different things, shown apart:
//  1. this device's changes waiting for the server (the offline queue, sync.js);
//  2. external calendars the server pulls from (connectors), each with its last
//     successful sync, its state, a readable error and «Синхронизировать сейчас».
import { api } from './api.js';
import { t, fmtDateTime } from './i18n.js';
import { esc, icon, chip, toast, errorMessage, setBusy } from './ui.js';
import { flushSync, syncState } from './sync.js';

export async function loadConnectors() {
  try { return await api('/api/v1/connectors'); } catch (error) { return { error }; }
}

const HEALTH_TONE = { CURRENT: 'ok', STALE: 'warn', UNAVAILABLE: 'danger' };

function connectorRow(c) {
  const error = c.error ? (t(`sync.connectorError.${c.error}`) === `sync.connectorError.${c.error}` ? t('sync.connectorError.other') : t(`sync.connectorError.${c.error}`)) : '';
  return `<div class="row static connector-row" data-connector="${esc(c.id)}">
    <span class="row-main"><strong>${esc(t(`sync.provider.${c.provider}`) === `sync.provider.${c.provider}` ? c.provider : t(`sync.provider.${c.provider}`))}</strong>
      <small>${esc(t('sync.lastSuccess'))}: ${esc(c.last_successful_sync_at ? fmtDateTime(c.last_successful_sync_at) : t('sync.never'))}</small>
      ${c.last_attempt_at ? `<small>${esc(t('sync.lastAttempt'))}: ${esc(fmtDateTime(c.last_attempt_at))}</small>` : ''}
      ${error ? `<small class="text-danger">${esc(error)}</small>` : ''}
      <span class="button-row">${c.can_sync ? `<button class="button small" data-action="connector-sync" data-id="${esc(c.id)}">${icon('refresh')}${esc(c.error ? t('common.retry') : t('sync.now'))}</button>` : ''}</span>
    </span>
    ${chip(t(`sync.health.${c.health}`), HEALTH_TONE[c.health] || 'muted')}
  </div>`;
}

export function syncSection(connectors) {
  const state = syncState();
  const local = `<div class="row static">
      <span class="row-main"><strong>${esc(t('sync.deviceTitle'))}</strong>
        <small>${esc(state.pending ? t('sync.pendingCount', { n: state.pending }) : t('sync.allSent'))}${state.conflicts ? ` · ${esc(t('sync.problemCount', { n: state.conflicts }))}` : ''}</small>
        <small>${esc(t('sync.lastSent'))}: ${esc(state.lastSyncedAt ? fmtDateTime(new Date(state.lastSyncedAt)) : t('sync.never'))}</small>
        <span class="button-row"><button class="button small" data-action="device-sync">${icon('refresh')}${esc(t('sync.retry'))}</button></span></span>
    </div>
    ${state.pending || state.conflicts ? `<button class="button ghost wide" data-action="sync-status">${esc(t('sync.details'))}</button>` : ''}`;
  let external;
  if (connectors?.error) external = `<p class="muted">${esc(errorMessage(connectors.error))}</p>`;
  else if (!connectors?.length) external = `<p class="help">${esc(t('sync.noConnectors'))}</p>`;
  else external = connectors.map(connectorRow).join('');
  return `<section class="section" data-sync-panel>
    <div class="section-head"><h2>${esc(t('sync.sectionTitle'))}</h2></div>
    <div class="card">${local}</div>
    <h3 class="group-head">${esc(t('sync.externalTitle'))}</h3>
    <div class="card">${external}</div>
  </section>`;
}

export const syncActions = {
  'device-sync': async (el, ctx) => {
    setBusy(el, true);
    try {
      await flushSync();
      const state = syncState();
      toast(state.pending ? t('sync.stillOffline') : t('sync.sentNow'), { error: Boolean(state.pending) });
    } catch (err) {
      toast(errorMessage(err), { error: true });
    }
    setBusy(el, false);
    ctx.rerender();
  },
  'connector-sync': async (el, ctx) => {
    setBusy(el, true);
    try {
      const result = await api(`/api/v1/connectors/${encodeURIComponent(el.dataset.id)}/sync`, { method: 'POST', timeoutMs: 120000 });
      toast(result.ok ? t('sync.connectorOk') : t('sync.connectorFailed'), { error: !result.ok });
    } catch (err) {
      toast(errorMessage(err), { error: true });
    }
    ctx.refresh();
  },
};
