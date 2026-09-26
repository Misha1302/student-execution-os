// A standalone reminder on its own screen (the target of a notification tap).
import { load } from '../store.js';
import { t, fmtDateTime, fmtRelative } from '../i18n.js';
import { esc, icon, empty, kv } from '../ui.js';
import { reminderStatusChip, reminderSheet, reminderAction, snoozeChoices, isOpen, hasAlarm, syncDeviceAlarms } from '../reminders.js';

export default {
  id: 'reminder',
  tab: 'tasks',
  detail: true,
  title: () => t('reminder.kind'),
  async load({ fresh, params }) {
    let result = await load('/api/v1/reminders', { fresh });
    if (!fresh && !result.data.some((x) => x.id === params[0])) result = await load('/api/v1/reminders', { fresh: true }).catch(() => result);
    syncDeviceAlarms();
    return { ...result, data: result.data.find((x) => x.id === params[0]) || null };
  },
  render(r) {
    if (!r) return empty(t('reminder.missing'), '', 'bell');
    const open = isOpen(r);
    return `<section class="detail-head">
        <div class="chips">${reminderStatusChip(r)}</div>
        <h2 class="detail-title">${esc(r.title)}</h2>
        ${r.note ? `<p class="muted pre">${esc(r.note)}</p>` : ''}
      </section>
      <section class="card"><dl class="kv-list">
        ${kv(t('reminder.when'), `${fmtDateTime(r.remind_at)} · ${fmtRelative(r.remind_at)}`)}
        ${kv(t('reminder.how'), t(`reminder.delivery.${r.delivery}`))}
        ${hasAlarm(r.delivery) ? kv(t('reminder.wake'), t(r.wake_check ? 'reminder.wake.yes' : 'reminder.wake.no')) : ''}
        ${r.acknowledged_at ? kv(t('reminder.upAt'), fmtDateTime(r.acknowledged_at)) : ''}
        ${r.awake_confirmed_at ? kv(t('reminder.awakeAt'), fmtDateTime(r.awake_confirmed_at)) : ''}
      </dl></section>
      <section class="detail-actions">
        ${open ? `<button class="button ok" data-action="rem-done">${icon('check')}${esc(t('reminder.markDone'))}</button>
          <button class="button" data-action="rem-snooze">${icon('clock')}${esc(t('notif.snooze'))}</button>
          <button class="button" data-action="rem-edit">${esc(t('task.edit'))}</button>
          <button class="button danger ghost" data-action="rem-cancel">${esc(t('reminder.cancel'))}</button>`
          : `<button class="button primary" data-action="rem-reopen">${icon('repeat')}${esc(t('reminder.reopen'))}</button>`}
        <button class="button danger ghost" data-action="rem-delete">${esc(t('lifecycle.delete'))}</button>
      </section>`;
  },
  actions: {
    'rem-done': (_el, ctx) => reminderAction(ctx.data, 'done'),
    'rem-cancel': (_el, ctx) => reminderAction(ctx.data, 'cancel'),
    'rem-reopen': (_el, ctx) => reminderAction(ctx.data, 'reopen'),
    'rem-delete': (_el, ctx) => reminderAction(ctx.data, 'delete'),
    'rem-edit': (_el, ctx) => reminderSheet(ctx.data),
    'rem-snooze': (_el, ctx) => snoozeChoices(ctx.data, (until) => reminderAction(ctx.data, 'snooze', { until })),
  },
};
