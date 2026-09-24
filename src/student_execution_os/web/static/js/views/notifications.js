import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, empty, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, setBusy, toast } from '../ui.js';
import { mutate } from '../actions.js';
import { shell } from '../actions.js';
import { queueOperation } from '../sync.js';

const TONE = { PENDING: 'accent', LEASED: 'warn', SENT: 'ok', NO_DEVICE: 'muted', CANCELLED: 'muted', DEAD: 'danger' };

function snoozeSheet(n) {
  const dialog = openSheet({
    eyebrow: n.stage,
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
      method: 'POST', body: { until },
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
    const sorted = [...list].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    return `<p class="help pad">${esc(t('notif.intro'))}</p>
      <div class="list">${sorted.map((n) => {
        const at = n.created_at;
        const canSnooze = !n.acted_at && !['CANCELLED', 'DEAD'].includes(n.delivery_state);
        return `<div class="row static">
          <span class="row-icon tone-${TONE[n.delivery_state] || 'muted'}">${icon('bell')}</span>
          <span class="row-main"><strong>${esc(n.title)}</strong><small>${esc(n.body)}</small><small>${esc(fmtDateTime(at))} · ${esc(fmtRelative(at))}</small>
            ${n.last_error ? `<small class="text-danger">${esc(n.last_error)}</small>` : ''}</span>
          <span class="row-side">${chip(n.delivery_state, TONE[n.delivery_state] || 'muted')}
            ${(n.actions || []).filter((a) => !a.id.startsWith('SNOOZE')).slice(0, 2).map((a) => `<button class="button small ghost" data-action="reminder-action" data-id="${esc(n.id)}" data-op="${esc(a.id)}">${esc(a.label)}</button>`).join('')}
            ${canSnooze ? `<button class="button small ghost" data-action="snooze-notification" data-id="${esc(n.id)}">${esc(t('notif.snooze'))}</button>` : ''}</span>
        </div>`;
      }).join('')}</div>`;
  },
  actions: {
    'snooze-notification'(el, ctx) {
      const n = (ctx.view._list || []).find((x) => x.id === el.dataset.id);
      if (n) snoozeSheet(n);
    },
    async 'reminder-action'(el, ctx) {
      const n = (ctx.view._list || []).find((x) => x.id === el.dataset.id);
      const taskId = n?.task_ids?.[0];
      if (!n) return;
      if (el.dataset.op === 'REPLAN') { shell.go('plan'); return; }
      if (!taskId || ['OPEN', 'RESCHEDULE'].includes(el.dataset.op)) { if (taskId) shell.go('task', { params: [taskId] }); return; }
      const type = el.dataset.op === 'START' ? 'task.start' : el.dataset.op === 'DONE' ? 'task.complete' : null;
      if (type) await mutate(async () => {
        const result = await queueOperation(type, taskId, {});
        return result.entity || result;
      });
    },
  },
};
