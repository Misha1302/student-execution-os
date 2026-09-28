import { api, apiUpload } from './api.js';
import { t, fmtDateTime } from './i18n.js';
import { esc, chip, toast, errorMessage, setBusy, confirmSheet } from './ui.js';
import { invalidate } from './store.js';

export async function loadAcademicSchedule() {
  try { return await api('/api/v1/settings/academic-schedule'); } catch (error) { return { error }; }
}

const TONE = { CURRENT: 'ok', STALE: 'warn', UNAVAILABLE: 'danger' };

export function academicScheduleSection(state) {
  if (state?.error) return `<section class="section"><div class="section-head"><h2>${esc(t('academic.title'))}</h2></div>
    <div class="card"><p class="muted">${esc(errorMessage(state.error))}</p></div></section>`;
  const connected = Boolean(state?.connected);
  const status = connected ? `<dl class="kv-list">
      <div class="kv"><dt>${esc(t('academic.source'))}</dt><dd>${esc(state.display_name || state.feed_host || t('academic.upload'))}</dd></div>
      <div class="kv"><dt>${esc(t('academic.mode'))}</dt><dd>${esc(t(`academic.mode.${state.mode}`))}</dd></div>
      <div class="kv"><dt>${esc(t('academic.lastSync'))}</dt><dd>${esc(state.last_successful_sync_at ? fmtDateTime(state.last_successful_sync_at) : t('sync.never'))}</dd></div>
    </dl>
    ${state.health ? chip(t(`sync.health.${state.health}`), TONE[state.health] || 'muted') : ''}
    ${state.latest_failure_reason ? `<p class="text-danger">${esc(t('academic.failure', { code: state.latest_failure_reason }))}</p>` : ''}
    <div class="button-row">
      ${state.mode === 'URL' ? `<button class="button" data-action="academic-sync">${esc(t('sync.now'))}</button>` : ''}
      <button class="button danger ghost" data-action="academic-disconnect">${esc(t('academic.disconnect'))}</button>
    </div>` : '';
  return `<section class="section" data-academic-schedule>
    <div class="section-head"><h2>${esc(t('academic.title'))}</h2></div>
    <div class="card form">
      <p class="muted">${esc(t('academic.help'))}</p>
      ${status}
      <label class="field"><span>${esc(t('academic.url'))}</span>
        <input type="url" data-academic-url autocomplete="off" spellcheck="false" placeholder="https://…/calendar.ics">
        <small class="help">${esc(t('academic.urlHelp'))}</small>
      </label>
      <label class="field"><span>${esc(t('academic.timezone'))}</span>
        <input data-academic-timezone value="${esc(state?.default_timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Moscow')}">
      </label>
      <button class="button" data-action="academic-connect">${esc(t('academic.connect'))}</button>
      <hr>
      <label class="field"><span>${esc(t('academic.file'))}</span>
        <input type="file" accept=".ics,text/calendar" data-academic-file>
        <small class="help">${esc(t('academic.fileHelp'))}</small>
      </label>
      <button class="button" data-action="academic-import">${esc(t('academic.import'))}</button>
      <p class="help">${esc(t('academic.hseReality'))}</p>
    </div>
  </section>`;
}

export const academicScheduleActions = {
  'academic-connect': async (button, ctx) => {
    const root = button.closest('[data-academic-schedule]');
    const url = root.querySelector('[data-academic-url]').value.trim();
    const defaultTimezone = root.querySelector('[data-academic-timezone]').value.trim();
    if (!url) { toast(t('academic.urlRequired'), { error: true }); return; }
    setBusy(button, true);
    try {
      await api('/api/v1/settings/academic-schedule', { method: 'PUT', timeoutMs: 120000, body: {
        url, default_timezone: defaultTimezone, display_name: t('academic.defaultName'), sync_interval_minutes: 60,
      } });
      invalidate();
      toast(t('academic.imported'));
      ctx.refresh();
    } catch (error) {
      toast(errorMessage(error), { error: true });
      setBusy(button, false);
    }
  },
  'academic-import': async (button, ctx) => {
    const root = button.closest('[data-academic-schedule]');
    const file = root.querySelector('[data-academic-file]').files?.[0];
    const defaultTimezone = root.querySelector('[data-academic-timezone]').value.trim();
    if (!file) { toast(t('academic.fileRequired'), { error: true }); return; }
    setBusy(button, true);
    try {
      await apiUpload('/api/v1/settings/academic-schedule/import', file, {
        method: 'POST', mimeType: 'text/calendar', filename: file.name, timeoutMs: 120000,
        extraHeaders: {
          'X-Calendar-Name': file.name.slice(0, 120),
          'X-Calendar-Timezone': defaultTimezone.slice(0, 80),
        },
      });
      invalidate();
      toast(t('academic.imported'));
      ctx.refresh();
    } catch (error) {
      toast(errorMessage(error), { error: true });
      setBusy(button, false);
    }
  },
  'academic-sync': async (button, ctx) => {
    setBusy(button, true);
    try {
      await api('/api/v1/settings/academic-schedule/sync', { method: 'POST', timeoutMs: 120000 });
      invalidate();
      toast(t('sync.connectorOk'));
      ctx.refresh();
    } catch (error) {
      toast(errorMessage(error), { error: true });
      setBusy(button, false);
    }
  },
  'academic-disconnect': async (_button, ctx) => {
    const ok = await confirmSheet({
      title: t('academic.disconnect'), body: `<p>${esc(t('academic.disconnectHelp'))}</p>`,
      confirmLabel: t('academic.disconnect'),
    });
    if (!ok) return;
    try {
      await api('/api/v1/settings/academic-schedule', { method: 'DELETE' });
      invalidate();
      toast(t('academic.disconnected'));
      ctx.refresh();
    } catch (error) { toast(errorMessage(error), { error: true }); }
  },
};
