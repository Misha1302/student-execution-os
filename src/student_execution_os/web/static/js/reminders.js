// Standalone reminders ("напомни купить хлеб завтра в 18", "разбуди меня в 7"):
// create, see, edit, snooze, finish. Every change is a queued reminder.* operation,
// so it works offline; alarms are handed to the Android device, which rings them from
// its own schedule (also offline and in Doze).
import { load, peek } from './store.js';
import { t, fmtDateTime, fmtTime, now } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, chip, confirmSheet } from './ui.js';
import { change, shell } from './actions.js';
import { newEntityId } from './sync.js';
import { syncAlarms, alarmsSupported } from './native.js';
import { reachWarning } from './health.js';

export const DELIVERIES = ['PUSH', 'ALARM', 'PUSH_AND_ALARM'];
export const hasAlarm = (delivery) => delivery === 'ALARM' || delivery === 'PUSH_AND_ALARM';
export const isOpen = (r) => r.status === 'SCHEDULED' || r.status === 'FIRED';

function at(offsetDays, hour, minute = 0) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(hour, minute, 0, 0);
  return d;
}

// Quick moments for "remind me": in an hour, this evening, tomorrow morning.
export function quickMoments() {
  const out = [['h1', t('resched.inHour'), new Date(Math.ceil((now().getTime() + 3600000) / 60000) * 60000)]];
  if (now().getHours() < 18) out.push(['evening', t('resched.evening'), at(0, 19)]);
  out.push(['morning', t('resched.tomorrowMorning'), at(1, 9)]);
  return out;
}

export function reminderPayload(fields) {
  const payload = { title: String(fields.title || '').trim(), remind_at: fields.remind_at, delivery: fields.delivery || 'PUSH' };
  if (fields.note) payload.note = fields.note;
  if (hasAlarm(payload.delivery)) {
    payload.wake_check = Boolean(fields.wake_check);
    payload.raise_volume = fields.raise_volume !== false;
  }
  if (fields.obligation_id) payload.obligation_id = fields.obligation_id;
  if (fields.assistant_batch_id) payload.assistant_batch_id = fields.assistant_batch_id;
  return payload;
}

export async function createReminder(fields, { toastText } = {}) {
  const payload = reminderPayload(fields);
  if (!payload.title) { toast(t('form.titleRequired'), { error: true }); return null; }
  if (!payload.remind_at || new Date(payload.remind_at) <= now()) { toast(t('form.remindPast'), { error: true }); return null; }
  const id = newEntityId('reminder');
  const result = await change('reminder.create', id, payload, {
    success: toastText ?? t(hasAlarm(payload.delivery) ? 'reminder.alarmSet' : 'reminder.set', { when: fmtDateTime(payload.remind_at) }),
  });
  if (result) syncDeviceAlarms();
  return result ? id : null;
}

export function deliveryChips(name, value) {
  return chipGroup(name, DELIVERIES.map((d) => [d, t(`reminder.delivery.${d}`)]), value || 'PUSH');
}

// The editable field set of a reminder (creation card and edit sheet).
export function reminderFieldsHtml(r) {
  return `<div class="form reminder-fields">
    <label class="field"><span>${esc(t('form.title'))}</span><input data-r="title" maxlength="300" value="${esc(r.title || '')}"></label>
    <label class="field"><span>${esc(t('reminder.when'))}</span><input type="datetime-local" data-r="at" value="${esc(localInputValue(r.remind_at))}"></label>
    <div class="field"><span>${esc(t('reminder.how'))}</span>${deliveryChips('r-delivery', r.delivery)}
      <small class="help">${esc(t('reminder.howHelp'))}</small></div>
    <div class="field ${hasAlarm(r.delivery) ? '' : 'hidden'}" data-r-alarm><span>${esc(t('reminder.wake'))}</span>
      ${chipGroup('r-wake', [['false', t('reminder.wake.no')], ['true', t('reminder.wake.yes')]], String(Boolean(r.wake_check)))}
      <small class="help">${esc(t('reminder.wakeHelp'))}</small>
      ${chipGroup('r-loud', [['true', t('reminder.loud.yes')], ['false', t('reminder.loud.no')]], String(r.raise_volume !== false))}</div>
    <label class="field"><span>${esc(t('reminder.note'))}</span><textarea data-r="note" rows="2" maxlength="2000">${esc(r.note || '')}</textarea></label>
    <div data-r-reach>${reachWarning({ alarm: hasAlarm(r.delivery) })}</div>
  </div>`;
}

export function bindReminderFields(root) {
  root.addEventListener('chipchange', (e) => {
    if (e.detail.name !== 'r-delivery') return;
    root.querySelector('[data-r-alarm]')?.classList.toggle('hidden', !hasAlarm(e.detail.value));
    const reach = root.querySelector('[data-r-reach]');
    if (reach) reach.innerHTML = reachWarning({ alarm: hasAlarm(e.detail.value) });
  });
}

export function readReminderFields(root) {
  const title = root.querySelector('[data-r="title"]').value.trim();
  const remindAt = isoFromLocalInput(root.querySelector('[data-r="at"]').value);
  const delivery = chipValue(root, 'r-delivery') || 'PUSH';
  return {
    title, remind_at: remindAt, delivery,
    wake_check: hasAlarm(delivery) && chipValue(root, 'r-wake') === 'true',
    raise_volume: hasAlarm(delivery) && chipValue(root, 'r-loud') !== 'false',
    note: root.querySelector('[data-r="note"]').value.trim() || null,
  };
}

export function reminderWhen(r) {
  return fmtDateTime(r.remind_at);
}

export function reminderStatusChip(r) {
  if (r.status === 'FIRED') return chip(t(r.acknowledged_at ? 'reminder.status.UP' : 'reminder.status.FIRED'), 'warn');
  if (r.status === 'DONE') return chip(t('reminder.status.DONE'), 'ok');
  if (r.status === 'CANCELLED') return chip(t('place.archive'), 'muted');
  return chip(t(hasAlarm(r.delivery) ? 'reminder.kindAlarm' : 'reminder.kind'), 'accent');
}

function changes(r, fields) {
  const out = {};
  for (const key of ['title', 'note', 'delivery', 'wake_check', 'raise_volume']) {
    if ((fields[key] ?? null) !== (r[key] ?? null)) out[key] = fields[key];
  }
  if (new Date(fields.remind_at).getTime() !== new Date(r.remind_at).getTime()) out.remind_at = fields.remind_at;
  if (!hasAlarm(fields.delivery)) { delete out.wake_check; delete out.raise_volume; }
  return out;
}

export async function reminderAction(r, action, extra = {}) {
  const ops = {
    done: ['reminder.done', {}, t('reminder.done.done')],
    cancel: ['reminder.cancel', {}, t('reminder.done.cancel')],
    reopen: ['reminder.reopen', {}, t('reminder.done.reopen')],
    snooze: ['reminder.snooze', { until: extra.until }, t('notif.snoozedUntil', { when: fmtDateTime(extra.until) })],
  };
  if (action === 'delete') {
    const ok = await confirmSheet({ title: t('reminder.deleteTitle'), body: `<p>${esc(t('reminder.deleteBody', { title: r.title }))}</p>`,
      confirmLabel: t('lifecycle.deleteConfirm'), cancelLabel: t('common.keep'), danger: true });
    if (!ok) return null;
    const done = await change('reminder.delete', r.id, {}, { success: t('lifecycle.done.delete') });
    syncDeviceAlarms();
    return done;
  }
  const [type, payload, success] = ops[action];
  const undo = { done: 'reminder.reopen', cancel: 'reminder.reopen', reopen: r.status === 'CANCELLED' ? 'reminder.cancel' : null }[action];
  const result = await change(type, r.id, payload, { success, undo: undo ? () => change(undo, r.id, {}).then(syncDeviceAlarms) : null });
  syncDeviceAlarms();
  return result;
}

export function snoozeChoices(r, onPick) {
  const dialog = openSheet({
    eyebrow: r.title,
    title: t('notif.snooze'),
    body: `<div class="chip-row">${[['10', t('reminder.in10')], ['60', t('notif.in60')], ['tomorrow', t('notif.tomorrow')], ['pick', t('form.custom')]]
      .map(([v, label]) => `<button type="button" class="chip-toggle" data-snooze="${v}">${esc(label)}</button>`).join('')}</div>
      <div class="field-row hidden" data-pick><input type="datetime-local" data-f="until" value="${esc(localInputValue(new Date(now().getTime() + 3600000)))}">
        <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button></div>`,
  });
  const finish = (until) => {
    if (!until || new Date(until) <= now()) { toast(t('resched.past'), { error: true }); return; }
    dialog.close('saved');
    onPick(until);
  };
  dialog.addEventListener('click', (e) => {
    const b = e.target.closest('[data-snooze]');
    if (!b) return;
    const v = b.dataset.snooze;
    if (v === 'pick') { dialog.querySelector('[data-pick]').classList.remove('hidden'); return; }
    finish(v === 'tomorrow' ? at(1, 9).toISOString() : new Date(now().getTime() + Number(v) * 60000).toISOString());
  });
  dialog.querySelector('[data-save]').addEventListener('click', () => finish(isoFromLocalInput(dialog.querySelector('[data-f="until"]').value)));
}

// Tap on a reminder: see it, change it, snooze or finish it.
export function reminderSheet(r) {
  const open = isOpen(r);
  const dialog = openSheet({
    eyebrow: t(hasAlarm(r.delivery) ? 'reminder.kindAlarm' : 'reminder.kind'),
    title: r.title,
    full: open,
    body: open ? `<p>${reminderStatusChip(r)}</p>${reminderFieldsHtml(r)}`
      : `<p>${reminderStatusChip(r)}</p><p class="muted">${esc(reminderWhen(r))}</p>${r.note ? `<p class="pre">${esc(r.note)}</p>` : ''}`,
    actions: open
      ? `<button type="button" class="button danger ghost" data-act="delete">${esc(t('lifecycle.delete'))}</button>
         <button type="button" class="button ghost" data-act="cancel">${esc(t('reminder.cancel'))}</button>
         <button type="button" class="button ghost" data-act="snooze">${esc(t('notif.snooze'))}</button>
         <button type="button" class="button ok" data-act="done">${icon('check')}${esc(t('reminder.markDone'))}</button>
         <button type="button" class="button primary" data-act="save">${esc(t('common.save'))}</button>`
      : `<button type="button" class="button danger ghost" data-act="delete">${esc(t('lifecycle.delete'))}</button>
         <button type="button" class="button primary" data-act="reopen">${icon('repeat')}${esc(t('reminder.reopen'))}</button>`,
  });
  if (open) bindReminderFields(dialog);
  dialog.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'save') {
      const fields = readReminderFields(dialog);
      if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
      if (!fields.remind_at) { toast(t('form.remindPast'), { error: true }); return; }
      const diff = changes(r, fields);
      if (diff.remind_at && new Date(diff.remind_at) <= now()) { toast(t('form.remindPast'), { error: true }); return; }
      dialog.close('saved');
      if (Object.keys(diff).length) {
        await change('reminder.update', r.id, diff, { success: t('reminder.saved') });
        syncDeviceAlarms();
      }
      return;
    }
    dialog.close(act);
    if (act === 'snooze') snoozeChoices(r, (until) => reminderAction(r, 'snooze', { until }));
    else await reminderAction(r, act);
  });
  return dialog;
}

// "Напомнить" about a task or an event: a reminder linked to it.
export function remindAboutSheet(entity) {
  const dialog = openSheet({
    eyebrow: entity.title,
    title: t('reminder.about'),
    body: `<div class="chip-row">${quickMoments().map(([v, label]) => `<button type="button" class="chip-toggle" data-when="${v}">${esc(label)}</button>`).join('')}
        <button type="button" class="chip-toggle" data-when="pick">${esc(t('form.custom'))}</button></div>
      <div class="field-row hidden" data-pick><input type="datetime-local" data-f="at" value="${esc(localInputValue(new Date(now().getTime() + 3600000)))}"></div>
      <div class="field"><span>${esc(t('reminder.how'))}</span>${deliveryChips('r-delivery', 'PUSH')}</div>
      <div data-r-reach>${reachWarning()}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save disabled>${esc(t('reminder.create'))}</button>`,
  });
  let when = null;
  const save = dialog.querySelector('[data-save]');
  dialog.addEventListener('chipchange', (e) => {
    if (e.detail.name === 'r-delivery') dialog.querySelector('[data-r-reach]').innerHTML = reachWarning({ alarm: hasAlarm(e.detail.value) });
  });
  dialog.addEventListener('click', (e) => {
    const b = e.target.closest('[data-when]');
    if (!b) return;
    dialog.querySelectorAll('[data-when]').forEach((x) => x.classList.toggle('on', x === b));
    if (b.dataset.when === 'pick') { dialog.querySelector('[data-pick]').classList.remove('hidden'); when = 'pick'; }
    else when = quickMoments().find(([v]) => v === b.dataset.when)?.[2].toISOString();
    save.disabled = !when;
  });
  save.addEventListener('click', async () => {
    const remindAt = when === 'pick' ? isoFromLocalInput(dialog.querySelector('[data-f="at"]').value) : when;
    const created = await createReminder({ title: entity.title, remind_at: remindAt, delivery: chipValue(dialog, 'r-delivery'),
      obligation_id: entity.kind === 'REMINDER' ? null : entity.id });
    if (created) dialog.close('saved');
  });
}

// ---- the phone's alarm schedule ------------------------------------------------------

// Hands the device every open alarm reminder of the account (queued ones included).
// An alarm answered on another phone («Я встал» there) is sent too, marked
// acknowledged: the device stops ringing it but keeps its own awake check.
export async function syncDeviceAlarms() {
  if (!alarmsSupported()) return;
  let reminders = peek('/api/v1/reminders');
  if (!reminders) {
    try { reminders = (await load('/api/v1/reminders', { cached: true })).data; } catch { return; }
  }
  const alarms = (reminders || []).filter((r) => hasAlarm(r.delivery) && isOpen(r))
    .map((r) => ({ id: r.id, remind_at: r.remind_at, title: r.title, wake_check: Boolean(r.wake_check),
      raise_volume: Boolean(r.raise_volume), status: r.status, acknowledged_at: r.acknowledged_at || null }));
  const labels = Object.fromEntries(['alarm_up', 'alarm_done', 'alarm_snooze', 'awake_title', 'awake_body', 'awake_ok', 'awake_wait', 'alarm_missed', 'test_title']
    .map((key) => [key, t(`alarm.${key}`)]));
  try {
    await syncAlarms(alarms, labels);
  } catch (err) { console.warn('alarm sync failed', err); }
}

export const reminderTimeShort = (r) => fmtTime(r.remind_at);
export const openReminderRoute = (id) => shell.go('reminder', { params: [id] });
