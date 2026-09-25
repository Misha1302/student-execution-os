import { load } from '../store.js';
import { t, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, empty, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast } from '../ui.js';
import { change, shell } from '../actions.js';

const ACTED = { SNOOZE: 'notif.acted.SNOOZE', DONE: 'notif.acted.DONE', START: 'notif.acted.START', RESCHEDULE: 'notif.acted.RESCHEDULE', PROGRESS: 'notif.acted.PROGRESS' };

// Snoozing is a reminder.snooze operation per task: it works offline, is replayed
// exactly once, and schedules the next reminder for the chosen moment.
async function snoozeTasks(n, until, success) {
  const ids = n.task_ids || [];
  let last = null;
  for (const [i, taskId] of ids.entries()) {
    last = await change('reminder.snooze', taskId, { until, reminder_message_id: n.id }, { success: i === ids.length - 1 ? success : null });
  }
  return last;
}

function snoozeSheet(n) {
  const dialog = openSheet({
    title: t('notif.snooze'),
    eyebrow: n.title,
    body: `${chipGroup('snooze', [['30', t('notif.in30')], ['60', t('notif.in60')], ['tomorrow', t('notif.tomorrow')], ['custom', t('form.custom')]], '60')}
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
    if (!until || new Date(until) <= now()) { toast(t('resched.past'), { error: true }); return; }
    dialog.close('saved');
    await snoozeTasks(n, until, t('notif.snoozedUntil', { when: fmtDateTime(until) }));
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
    return `<div class="list">${sorted.map((n) => {
      const at = n.created_at;
      const answered = Boolean(n.acted_at);
      const taskAction = (n.actions || []).filter((a) => ['START', 'DONE', 'OPEN', 'RESCHEDULE'].includes(a.id)).slice(0, 2);
      return `<div class="row static">
          <span class="row-icon tone-${answered ? 'muted' : 'accent'}">${icon('bell')}</span>
          <span class="row-main"><strong>${esc(n.title)}</strong><small>${esc(n.body)}</small><small>${esc(fmtDateTime(at))} · ${esc(fmtRelative(at))}</small></span>
          <span class="row-side">${answered ? chip(t(ACTED[n.acted_action] || 'notif.acted.SEEN'), 'muted') : `
            ${taskAction.map((a) => `<button class="button small ghost" data-action="reminder-action" data-id="${esc(n.id)}" data-op="${esc(a.id)}">${esc(a.label)}</button>`).join('')}
            <button class="button small ghost" data-action="snooze-notification" data-id="${esc(n.id)}">${esc(t('notif.snooze'))}</button>`}</span>
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
      if (!taskId || el.dataset.op === 'OPEN') { if (taskId) shell.go('task', { params: [taskId] }); else shell.go('today'); return; }
      if (el.dataset.op === 'RESCHEDULE') { location.hash = `#/task/${encodeURIComponent(taskId)}?step=reschedule`; return; }
      const type = el.dataset.op === 'START' ? 'task.start' : 'task.complete';
      await change(type, taskId, { reminder_message_id: n.id }, { success: t(el.dataset.op === 'START' ? 'today.started' : 'lifecycle.done.complete') });
    },
  },
};
