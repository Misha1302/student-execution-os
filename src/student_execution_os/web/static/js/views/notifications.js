import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, empty, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, setBusy, toast } from '../ui.js';
import { mutate } from '../actions.js';

const TONE = { PENDING: 'accent', SNOOZED: 'warn', DELIVERED: 'ok', SUPPRESSED: 'muted', FAILED: 'danger' };

function snoozeSheet(n) {
  const dialog = openSheet({
    eyebrow: code('notif', n.kind),
    title: t('notif.snooze'),
    body: `${chipGroup('snooze', [['15', t('notif.in15')], ['60', t('notif.in60')], ['tomorrow', t('notif.tomorrow')], ['custom', t('form.custom')]], '60')}
      <input type="datetime-local" data-f="until" class="hidden" value="${esc(localInputValue(new Date(now().getTime() + 3600000)))}">
      <p class="help">${esc(t('notif.snoozeHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('notif.snooze'))}</button>`,
  });
  dialog.addEventListener('chipchange', (e) => {
    dialog.querySelector('[data-f="until"]').classList.toggle('hidden', e.detail.value !== 'custom');
  });
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const choice = chipValue(dialog, 'snooze');
    let until;
    if (choice === 'custom') until = isoFromLocalInput(dialog.querySelector('[data-f="until"]').value);
    else if (choice === 'tomorrow') { const d = now(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); until = d.toISOString(); }
    else until = new Date(now().getTime() + Number(choice) * 60000).toISOString();
    if (!until) { toast(t('form.deadlineRequired'), { error: true }); return; }
    setBusy(e.currentTarget, true);
    await mutate(() => api(`/api/v1/notifications/${encodeURIComponent(n.id)}/snooze`, {
      method: 'POST', body: { until, expected_version: n.version },
    }), { success: t('notif.snoozed') });
    dialog.close('saved');
  });
}

export default {
  id: 'notifications',
  tab: 'more',
  detail: true,
  title: () => t('nav.notifications'),
  load: ({ fresh }) => load('/api/v1/notifications', { fresh }),
  render(list) {
    this._list = list;
    if (!list.length) return empty(t('notif.empty'), t('notif.emptyHint'), 'bell');
    const sorted = [...list].sort((a, b) => new Date(b.snoozed_until || b.scheduled_for) - new Date(a.snoozed_until || a.scheduled_for));
    return `<p class="help pad">${esc(t('notif.intro'))}</p>
      <div class="list">${sorted.map((n) => {
        const at = n.snoozed_until || n.scheduled_for;
        const canSnooze = !['DELIVERED', 'SUPPRESSED'].includes(n.state);
        return `<div class="row static">
          <span class="row-icon tone-${TONE[n.state] || 'muted'}">${icon('bell')}</span>
          <span class="row-main"><strong>${esc(code('notif', n.kind))}</strong><small>${esc(fmtDateTime(at))} · ${esc(fmtRelative(at))}</small>
            ${n.last_error ? `<small class="text-danger">${esc(n.last_error)}</small>` : ''}</span>
          <span class="row-side">${chip(code('notifState', n.state), TONE[n.state] || 'muted')}
            ${canSnooze ? `<button class="button small ghost" data-action="snooze-notification" data-id="${esc(n.id)}">${esc(t('notif.snooze'))}</button>` : ''}</span>
        </div>`;
      }).join('')}</div>`;
  },
  actions: {
    'snooze-notification'(el, ctx) {
      const n = (ctx.view._list || []).find((x) => x.id === el.dataset.id);
      if (n) snoozeSheet(n);
    },
  },
};
