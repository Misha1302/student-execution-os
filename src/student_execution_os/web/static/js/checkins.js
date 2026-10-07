// Check-ins and recurring reminders on the device (schema v31, ADR 0034).
//
// A check-in occurrence shows what really happened («Принял в 09:04»), never a
// schema state. Outcome buttons queue checkin.occurrence.* operations; «позже»
// snoozes only the prompt (reminder.snooze), so the day stays open.

import { t, fmtTime, fmtDateTime, fmtDuration, getLocale } from './i18n.js';
import { esc, icon, chip, openSheet, chipGroup, chipValue, toast, confirmSheet, actionSheet, setBusy, localInputValue } from './ui.js';
import { change, shell } from './actions.js';
import { newEntityId } from './sync.js';
import { syncDeviceAlarms } from './reminders.js';

const WEEKDAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
const deviceTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

export const isMedication = (x) => (x.checkin_kind || x.kind) === 'MEDICATION';
export const isQuota = (x) => (x.checkin_kind || x.kind) === 'QUOTA';

// «Каждый день», «По будням», «Пн, Ср, Пт», «Раз в две недели»…
export function ruleLabel(rule) {
  const parts = Object.fromEntries(String(rule || '').split(';').filter(Boolean).map((x) => x.split('=')));
  const interval = Number(parts.INTERVAL || 1);
  if (parts.BYDAY) {
    const days = parts.BYDAY.split(',');
    if (days.join(',') === 'MO,TU,WE,TH,FR') return t('checkin.repeat.weekdays');
    if (days.join(',') === 'SA,SU') return t('checkin.repeat.weekends');
    const names = days.map((d) => t(`checkin.day.${d}`)).join(', ');
    return interval === 2 ? t('checkin.repeat.everyOtherWeekOn', { days: names }) : names;
  }
  if (parts.FREQ === 'DAILY') return interval === 1 ? t('checkin.repeat.daily') : t('checkin.repeat.everyNDays', { n: interval });
  return interval === 1 ? t('checkin.repeat.weekly') : t('checkin.repeat.everyNWeeks', { n: interval });
}

// What the user sees about one occurrence, in their words.
export function outcomeText(occ) {
  const med = isMedication(occ);
  switch (occ.status) {
    case 'DONE':
      if (isQuota(occ)) return t('checkin.status.quotaDone', { done: occ.quantity_done, target: occ.target_quantity, unit: occ.unit || '' });
      return t(med ? 'checkin.status.taken' : 'checkin.status.done', { time: occ.occurred_at ? fmtTime(occ.occurred_at) : '' });
    case 'SKIPPED':
      return t(med ? 'checkin.status.notTaken' : 'checkin.status.skipped');
    case 'MISSED':
      return isQuota(occ) && occ.quantity_done
        ? t('checkin.status.quotaPartial', { done: occ.quantity_done, target: occ.target_quantity, unit: occ.unit || '' })
        : t('checkin.status.missed');
    case 'CANCELLED':
      return t('checkin.status.cancelled');
    default:
      if (isQuota(occ)) return t('checkin.status.quotaOpen', { done: occ.quantity_done || 0, target: occ.target_quantity, unit: occ.unit || '' });
      return new Date(occ.scheduled_at) > new Date() ? t('checkin.status.planned') : t('checkin.status.waiting');
  }
}

const TONE = { DONE: 'ok', SKIPPED: 'muted', MISSED: 'warn', CANCELLED: 'muted', PENDING: 'accent' };

function dataAttrs(occ) {
  return `data-template="${esc(occ.template_id)}" data-rid="${esc(occ.original_recurrence_id)}" data-kind="${esc(occ.checkin_kind)}"
    data-title="${esc(occ.title)}" data-target="${esc(occ.target_quantity ?? '')}" data-done="${esc(occ.quantity_done ?? 0)}"
    data-unit="${esc(occ.unit || '')}" data-reminder="${esc(occ.reminder_id || '')}" data-scheduled="${esc(occ.scheduled_local || '')}"`;
}

// One row with the buttons that record the outcome.
// When the plan keeps time for this quota today ("по плану 11:00–11:45"), from the
// planner's derived QUOTA blocks; the count itself stays on the occurrence.
function plannedSlots(occ, quotaBlocks = []) {
  const mine = quotaBlocks.filter((b) => b.template_id === occ.template_id && b.original_recurrence_id === occ.original_recurrence_id);
  if (!mine.length) return '';
  return t('checkin.plannedAt', { slots: mine.map((b) => `${fmtTime(b.starts_at)}–${fmtTime(b.ends_at)}`).join(', ') });
}

export function checkinRow(occ, { showDate = false, quotaBlocks = [] } = {}) {
  const med = isMedication(occ);
  const open = occ.status === 'PENDING' || occ.status === 'MISSED';
  const label = `${med ? '💊 ' : ''}${occ.title}`;
  const when = showDate ? fmtDateTime(occ.scheduled_at) : fmtTime(occ.scheduled_at);
  const extra = [occ.dose_text, open && isQuota(occ) ? plannedSlots(occ, quotaBlocks) : '',
    occ.moved_to_local ? t('checkin.movedHint') : '', occ._pending ? t('checkin.pendingSync') : '']
    .filter(Boolean).join(' · ');
  const buttons = !open ? `<button class="button small ghost" data-action="checkin-menu" ${dataAttrs(occ)} aria-label="${esc(t('checkin.changeOutcome', { title: occ.title }))}">${esc(t('checkin.change'))}</button>`
    : isQuota(occ)
      ? `<button class="button small primary" data-action="checkin-progress" ${dataAttrs(occ)}>${esc(t('checkin.addCount'))}</button>
         <button class="button small ghost" data-action="checkin-menu" ${dataAttrs(occ)} aria-label="${esc(t('checkin.more', { title: occ.title }))}">${esc(t('capture.more'))}</button>`
      : `<button class="button small primary" data-action="checkin-done" ${dataAttrs(occ)} aria-label="${esc(t(med ? 'checkin.takenAria' : 'checkin.doneAria', { title: occ.title }))}">${esc(t(med ? 'checkin.taken' : 'checkin.done'))}</button>
         <button class="button small ghost" data-action="checkin-menu" ${dataAttrs(occ)} aria-label="${esc(t('checkin.more', { title: occ.title }))}">${esc(t('capture.more'))}</button>`;
  return `<div class="row static checkin-row" data-anchor="checkin:${esc(occ.template_id)}:${esc(occ.original_recurrence_id)}">
    <span class="row-time"><strong>${esc(when)}</strong></span>
    <span class="row-main"><strong>${esc(label)}</strong><small>${esc(outcomeText(occ))}${extra ? ` · ${esc(extra)}` : ''}</small></span>
    <span class="row-aside checkin-actions">${open ? '' : chip(t(`checkin.chip.${occ.status}`), TONE[occ.status] || 'muted')}${buttons}</span>
  </div>`;
}

export function todayCheckinsSection(items = [], { quotaBlocks = [] } = {}) {
  if (!items.length) return '';
  const counted = items.filter((x) => x.status !== 'CANCELLED');
  const done = counted.filter((x) => x.status === 'DONE').length;
  return `<section class="section" data-checkins>
    <div class="section-head"><h2>${esc(t('checkin.today'))}</h2><span class="muted">${esc(t('checkin.progress', { done, total: counted.length }))}</span>
      <button class="link" data-nav="checkins">${esc(t('checkin.all'))}</button></div>
    <div class="list">${items.map((x) => checkinRow(x, { quotaBlocks })).join('')}</div>
  </section>`;
}

function occurrenceFrom(el) {
  const d = el.dataset;
  return { template_id: d.template, original_recurrence_id: d.rid, checkin_kind: d.kind, title: d.title,
    target_quantity: d.target === '' ? null : Number(d.target), quantity_done: Number(d.done || 0), unit: d.unit || null,
    reminder_id: d.reminder || null, scheduled_local: d.scheduled || d.rid };
}

const identity = (occ) => ({ template_id: occ.template_id, original_recurrence_id: occ.original_recurrence_id });

// An answer in the app also stops the phone's alarm for that occurrence.
const thenAlarms = (result) => { syncDeviceAlarms(); return result; };

export function markDone(occ) {
  return change('checkin.occurrence.done', occ.template_id, { ...identity(occ), occurred_at: new Date().toISOString() }, {
    success: t(isMedication(occ) ? 'checkin.toast.taken' : 'checkin.toast.done', { title: occ.title }),
    undo: () => change('checkin.occurrence.reopen', occ.template_id, identity(occ)).then(thenAlarms),
  }).then(thenAlarms);
}

export function markSkipped(occ) {
  return change('checkin.occurrence.skip', occ.template_id, identity(occ), {
    success: t(isMedication(occ) ? 'checkin.toast.notTaken' : 'checkin.toast.skipped', { title: occ.title }),
    undo: () => change('checkin.occurrence.reopen', occ.template_id, identity(occ)).then(thenAlarms),
  }).then(thenAlarms);
}

function quotaSheet(occ) {
  const left = Math.max(0, (occ.target_quantity || 0) - (occ.quantity_done || 0));
  const options = [...new Set([1, 5, 10, left].filter((n) => n > 0))].sort((a, b) => a - b)
    .map((n) => [String(n), n === left ? t('checkin.allLeft', { n }) : `+${n}`]);
  const dialog = openSheet({
    eyebrow: occ.title,
    title: t('checkin.addCountTitle'),
    body: `<p class="muted">${esc(t('checkin.status.quotaOpen', { done: occ.quantity_done || 0, target: occ.target_quantity, unit: occ.unit || '' }))}</p>
      <div class="field"><span>${esc(t('checkin.howMany'))}</span>${chipGroup('quota-count', options, options[0]?.[0] || '1')}</div>
      <label class="field"><span>${esc(t('checkin.orExact'))}</span><input type="number" inputmode="numeric" min="1" max="100000" data-quota-exact></label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('progress.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const exact = Number(dialog.querySelector('[data-quota-exact]').value || 0);
    const count = exact > 0 ? Math.floor(exact) : Number(chipValue(dialog, 'quota-count') || 0);
    if (!count) return;
    dialog.close('saved');
    await change('checkin.occurrence.progress', occ.template_id, { ...identity(occ), count, occurred_at: new Date().toISOString() }, {
      success: t('checkin.toast.counted', { done: (occ.quantity_done || 0) + count, target: occ.target_quantity, unit: occ.unit || '' }),
    });
  });
}

function moveSheet(occ) {
  const dialog = openSheet({
    eyebrow: occ.title,
    title: t('checkin.moveTitle'),
    body: `<label class="field"><span>${esc(t('checkin.moveTo'))}</span>
      <input type="datetime-local" data-move value="${esc(String(occ.scheduled_local || '').slice(0, 16))}"></label>
      <p class="help">${esc(t('checkin.moveHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const value = dialog.querySelector('[data-move]').value;
    if (!value) return;
    dialog.close('saved');
    await change('checkin.occurrence.move', occ.template_id, { ...identity(occ), target_local: value }, {
      success: t('checkin.toast.moved', { time: value.slice(11, 16) }),
    });
  });
}

async function occurrenceMenu(occ) {
  const med = isMedication(occ);
  const resolved = !['PENDING', 'MISSED'].includes(occ.status) || occ.status === 'MISSED';
  const items = [];
  if (occ.status === 'PENDING' || occ.status === 'MISSED') {
    if (isQuota(occ)) items.push({ id: 'done', icon: 'check', label: t('checkin.allDone') });
    items.push({ id: 'skip', icon: 'x', label: t(med ? 'checkin.notTaken' : 'checkin.skip') });
  }
  if (occ.status === 'PENDING') {
    if (occ.reminder_id) items.push({ id: 'snooze', icon: 'clock', label: t('checkin.snooze15') });
    items.push({ id: 'move', icon: 'calendar', label: t('checkin.moveOnly') });
    items.push({ id: 'cancel', icon: 'x', label: t('checkin.cancelDay'), hint: t('checkin.cancelDayHint') });
  }
  if (resolved && occ.status !== 'PENDING') items.push({ id: 'reopen', icon: 'refresh', label: t('checkin.reopen') });
  items.push({ id: 'open', icon: 'repeat', label: t('checkin.openSeries') });
  const choice = await actionSheet({ title: occ.title, items });
  if (choice === 'done') return markDone(occ);
  if (choice === 'skip') return markSkipped(occ);
  if (choice === 'snooze') {
    return change('reminder.snooze', occ.reminder_id, { until: new Date(Date.now() + 15 * 60000).toISOString() },
      { success: t('checkin.toast.snoozed', { time: fmtTime(new Date(Date.now() + 15 * 60000)) }) });
  }
  if (choice === 'move') return moveSheet(occ);
  if (choice === 'cancel') return change('checkin.occurrence.cancel', occ.template_id, identity(occ), { success: t('checkin.toast.cancelled') });
  if (choice === 'reopen') return change('checkin.occurrence.reopen', occ.template_id, identity(occ), { success: t('checkin.toast.reopened') });
  if (choice === 'open') return shell.go('checkin', { params: [occ.template_id] });
  return null;
}

export const CHECKIN_ACTIONS = {
  'checkin-done': (el) => markDone(occurrenceFrom(el)),
  'checkin-progress': (el) => quotaSheet(occurrenceFrom(el)),
  'checkin-menu': (el) => occurrenceMenu(occurrenceFrom(el)),
  'open-checkin': (el) => shell.go('checkin', { params: [el.dataset.id] }),
  'checkin-new': () => createCheckinSheet(),
};

// ---- creation ------------------------------------------------------------------------

function nextLocal(timeValue) {
  const [hh, mm] = String(timeValue || '09:00').split(':').map(Number);
  const now = new Date();
  const at = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hh || 0, mm || 0);
  if (at <= now) at.setDate(at.getDate() + 1);
  return localInputValue(at);
}

export function repeatRule(choice, days = []) {
  if (choice === 'WEEKDAYS') return 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR';
  if (choice === 'WEEKLY') return 'FREQ=WEEKLY';
  if (choice === 'DAYS' && days.length) return `FREQ=WEEKLY;BYDAY=${WEEKDAYS.filter((d) => days.includes(d)).join(',')}`;
  return 'FREQ=DAILY';
}

// First occurrence on/after now that matches the rule (local civil time).
export function firstStart(timeValue, choice, days = []) {
  const [hh, mm] = String(timeValue || '09:00').split(':').map(Number);
  const now = new Date();
  const allowed = choice === 'WEEKDAYS' ? ['MO', 'TU', 'WE', 'TH', 'FR'] : choice === 'DAYS' ? days : null;
  for (let i = 0; i < 14; i += 1) {
    const at = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, hh || 0, mm || 0);
    if (at <= now) continue;
    if (allowed && !allowed.includes(WEEKDAYS[(at.getDay() + 6) % 7])) continue;
    return localInputValue(at);
  }
  return nextLocal(timeValue);
}

export function createCheckinSheet(prefill = {}) {
  const zone = deviceTimeZone();
  const kind = prefill.kind || 'MEDICATION';
  const dialog = openSheet({
    title: t('checkin.new'),
    body: `<div class="field"><span>${esc(t('checkin.what'))}</span>${chipGroup('checkin-kind', [
      ['MEDICATION', t('checkin.kind.MEDICATION')], ['ROUTINE', t('checkin.kind.ROUTINE')],
      ['QUOTA', t('checkin.kind.QUOTA')], ['REMINDER', t('checkin.kind.REMINDER')]], kind)}</div>
      <p class="help" data-kind-help>${esc(t(`checkin.kindHelp.${kind}`))}</p>
      <label class="field"><span data-title-label>${esc(t(kind === 'MEDICATION' ? 'checkin.medName' : 'form.title'))}</span>
        <input data-checkin-title maxlength="300" autocomplete="off" value="${esc(prefill.title || '')}"></label>
      <label class="field" data-only="MEDICATION"><span>${esc(t('checkin.dose'))}</span>
        <input data-checkin-dose maxlength="200" autocomplete="off" placeholder="${esc(t('checkin.dosePlaceholder'))}" value="${esc(prefill.dose_text || '')}"></label>
      <div class="field-row" data-only="QUOTA">
        <label class="field"><span>${esc(t('checkin.target'))}</span><input type="number" inputmode="numeric" min="1" max="100000" data-checkin-target value="${esc(prefill.target_quantity || '')}"></label>
        <label class="field"><span>${esc(t('checkin.unit'))}</span><input maxlength="40" data-checkin-unit placeholder="${esc(t('checkin.unitPlaceholder'))}" value="${esc(prefill.unit || '')}"></label>
      </div>
      <label class="field" data-only="QUOTA"><span>${esc(t('checkin.pace'))}</span>
        <input type="number" inputmode="decimal" min="0.1" max="1440" step="0.1" data-checkin-pace placeholder="${esc(t('checkin.pacePlaceholder'))}"></label>
      <p class="help" data-only="QUOTA">${esc(t('checkin.paceHelp'))}</p>
      <label class="field"><span>${esc(t('checkin.time'))}</span><input type="time" data-checkin-time value="${esc(prefill.time || '09:00')}"></label>
      <div class="field"><span>${esc(t('routine.repeat'))}</span>${chipGroup('checkin-repeat', [
        ['DAILY', t('checkin.repeat.daily')], ['WEEKDAYS', t('checkin.repeat.weekdays')],
        ['WEEKLY', t('checkin.repeat.weekly')], ['DAYS', t('checkin.repeat.pickDays')]], prefill.repeat || 'DAILY')}</div>
      <div class="chip-group multi" data-days hidden>${WEEKDAYS.map((d) => `<button type="button" class="chip-toggle" data-day="${d}" aria-pressed="false">${esc(t(`checkin.day.${d}`))}</button>`).join('')}</div>
      <div class="field" data-hide="REMINDER"><span>${esc(t('checkin.remindLabel'))}</span>${chipGroup('checkin-remind', [
        ['PUSH', t('checkin.remind.push')], ['PUSH_AND_ALARM', t('checkin.remind.alarm')], ['NONE', t('checkin.remind.none')]], 'PUSH')}</div>
      <p class="help" data-only="MEDICATION">${esc(t('checkin.medicationNotice'))}</p>
      <p class="help">${esc(t('routine.timezone', { zone }))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-checkin-create>${esc(t('compose.create'))}</button>`,
  });
  const sync = () => {
    const current = chipValue(dialog, 'checkin-kind');
    dialog.querySelectorAll('[data-only]').forEach((el) => { el.hidden = el.dataset.only !== current; });
    dialog.querySelectorAll('[data-hide]').forEach((el) => { el.hidden = el.dataset.hide === current; });
    dialog.querySelector('[data-kind-help]').textContent = t(`checkin.kindHelp.${current}`);
    dialog.querySelector('[data-title-label]').textContent = t(current === 'MEDICATION' ? 'checkin.medName' : 'form.title');
    dialog.querySelector('[data-days]').hidden = chipValue(dialog, 'checkin-repeat') !== 'DAYS';
  };
  dialog.addEventListener('chipchange', sync);
  dialog.querySelectorAll('[data-day]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const on = b.getAttribute('aria-pressed') !== 'true';
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('on', on);
  }));
  sync();
  dialog.querySelector('[data-checkin-title]').focus();
  dialog.querySelector('[data-checkin-create]').addEventListener('click', async (event) => {
    const current = chipValue(dialog, 'checkin-kind');
    const title = String(dialog.querySelector('[data-checkin-title]').value || '').trim();
    const time = dialog.querySelector('[data-checkin-time]').value || '09:00';
    const repeat = chipValue(dialog, 'checkin-repeat');
    const days = [...dialog.querySelectorAll('[data-day][aria-pressed="true"]')].map((b) => b.dataset.day);
    if (!title) { toast(t('checkin.needTitle'), { error: true }); return; }
    if (repeat === 'DAYS' && !days.length) { toast(t('checkin.needDays'), { error: true }); return; }
    const base = { title, dtstart_local: firstStart(time, repeat, days), recurrence_rule: repeatRule(repeat, days), timezone_name: zone };
    setBusy(event.currentTarget, true);
    let result;
    if (current === 'REMINDER') {
      result = await change('reminder_series.create', newEntityId('series'), { ...base, delivery: 'PUSH' },
        { success: t('checkin.toast.seriesCreated', { title, rule: ruleLabel(base.recurrence_rule) }) });
    } else {
      const remind = chipValue(dialog, 'checkin-remind');
      const payload = { ...base, kind: current, remind: remind !== 'NONE', delivery: remind === 'NONE' ? 'PUSH' : remind };
      if (current === 'MEDICATION') {
        const dose = String(dialog.querySelector('[data-checkin-dose]').value || '').trim();
        if (dose) payload.dose_text = dose;
      }
      if (current === 'QUOTA') {
        const target = Number(dialog.querySelector('[data-checkin-target]').value || 0);
        if (!target) { setBusy(event.currentTarget, false); toast(t('checkin.needTarget'), { error: true }); return; }
        payload.target_quantity = Math.floor(target);
        const unit = String(dialog.querySelector('[data-checkin-unit]').value || '').trim();
        if (unit) payload.unit = unit;
        const pace = Number(dialog.querySelector('[data-checkin-pace]').value || 0);
        if (pace > 0) payload.unit_effort_seconds = Math.round(pace * 60);
      }
      result = await change('checkin.create', newEntityId('checkin'), payload,
        { success: t('checkin.toast.created', { title, rule: ruleLabel(base.recurrence_rule) }) });
    }
    if (result) dialog.close('created'); else setBusy(event.currentTarget, false);
  });
  return dialog;
}

// ---- series (template) management -------------------------------------------------------

export async function endCheckin(template) {
  const ok = await confirmSheet({
    title: t('checkin.endTitle'), body: `<p>${esc(t('checkin.endBody', { title: template.title }))}</p>`,
    confirmLabel: t('checkin.end'), cancelLabel: t('common.keep'),
  });
  if (ok) await change('checkin.end', template.id, { expected_version: template.version || undefined }, { success: t('checkin.toast.ended') });
}

export async function deleteCheckin(template) {
  const ok = await confirmSheet({
    title: t('checkin.deleteTitle'), body: `<p>${esc(t('checkin.deleteBody', { title: template.title }))}</p>`,
    confirmLabel: t('lifecycle.deleteConfirm'), cancelLabel: t('common.keep'), danger: true,
  });
  if (!ok) return;
  const done = await change('checkin.delete', template.id, {}, { success: t('checkin.toast.deleted') });
  if (done) shell.go('checkins');
}

export function editCheckinSheet(template) {
  const dialog = openSheet({
    eyebrow: template.title,
    title: t('checkin.edit'),
    body: `<label class="field"><span>${esc(t(isMedication(template) ? 'checkin.medName' : 'form.title'))}</span>
        <input data-edit-title maxlength="300" value="${esc(template.title)}"></label>
      ${isMedication(template) ? `<label class="field"><span>${esc(t('checkin.dose'))}</span><input data-edit-dose maxlength="200" value="${esc(template.dose_text || '')}"></label>` : ''}
      ${isQuota(template) ? `<div class="field-row"><label class="field"><span>${esc(t('checkin.target'))}</span><input type="number" min="1" data-edit-target value="${esc(template.target_quantity || '')}"></label>
        <label class="field"><span>${esc(t('checkin.unit'))}</span><input maxlength="40" data-edit-unit value="${esc(template.unit || '')}"></label></div>
        <label class="field"><span>${esc(t('checkin.pace'))}</span><input type="number" min="0.1" step="0.1" data-edit-pace value="${esc(template.unit_effort_seconds ? (template.unit_effort_seconds / 60) : '')}"></label>` : ''}
      <div class="field"><span>${esc(t('checkin.remindLabel'))}</span>${chipGroup('edit-remind', [
        ['PUSH', t('checkin.remind.push')], ['PUSH_AND_ALARM', t('checkin.remind.alarm')], ['NONE', t('checkin.remind.none')]],
        template.remind ? template.delivery : 'NONE')}</div>
      <label class="field"><span>${esc(t('checkin.followup'))}</span><input type="number" min="5" max="720" data-edit-followup value="${esc(template.followup_minutes || '')}" placeholder="${esc(t('checkin.followupNone'))}"></label>
      <p class="help">${esc(t('checkin.timeChangeHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const payload = { expected_version: template.version || undefined };
    const title = String(dialog.querySelector('[data-edit-title]').value || '').trim();
    if (title && title !== template.title) payload.title = title;
    const dose = dialog.querySelector('[data-edit-dose]');
    if (dose && dose.value.trim() !== (template.dose_text || '')) payload.dose_text = dose.value.trim() || null;
    const target = dialog.querySelector('[data-edit-target]');
    if (target && Number(target.value) && Number(target.value) !== template.target_quantity) payload.target_quantity = Math.floor(Number(target.value));
    const unit = dialog.querySelector('[data-edit-unit]');
    if (unit && unit.value.trim() !== (template.unit || '')) payload.unit = unit.value.trim() || null;
    const pace = dialog.querySelector('[data-edit-pace]');
    if (pace) {
      const seconds = Number(pace.value) > 0 ? Math.round(Number(pace.value) * 60) : null;
      if (seconds !== (template.unit_effort_seconds ?? null)) payload.unit_effort_seconds = seconds;
    }
    const remind = chipValue(dialog, 'edit-remind');
    if ((remind !== 'NONE') !== Boolean(template.remind)) payload.remind = remind !== 'NONE';
    if (remind !== 'NONE' && remind !== template.delivery) payload.delivery = remind;
    const followup = Number(dialog.querySelector('[data-edit-followup]').value || 0) || null;
    if (followup !== (template.followup_minutes ?? null)) payload.followup_minutes = followup;
    dialog.close('saved');
    if (Object.keys(payload).filter((k) => k !== 'expected_version').length) {
      await change('checkin.update', template.id, payload, { success: t('checkin.toast.updated') });
    }
  });
}

// «С какого дня изменить время»: this and following days, history stays as recorded.
export function changeTimeSheet(template, fromRid) {
  const dialog = openSheet({
    eyebrow: template.title,
    title: t('checkin.changeTime'),
    body: `<label class="field"><span>${esc(t('checkin.newTime'))}</span><input type="time" data-new-time value="${esc(String(template.dtstart_local || '').slice(11, 16))}"></label>
      <p class="help">${esc(t('checkin.changeTimeHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const time = dialog.querySelector('[data-new-time]').value;
    if (!time || !fromRid) return;
    dialog.close('saved');
    await change('checkin.split', newEntityId('checkin'), {
      template_id: template.id, original_recurrence_id: fromRid, start_local: `${fromRid.slice(0, 10)}T${time}`,
      expected_version: template.version || undefined,
    }, { success: t('checkin.toast.timeChanged', { time }) });
    shell.go('checkins');
  });
}

export function seriesRow(series) {
  const next = series.next?.remind_at;
  return `<div class="row static" data-anchor="series:${esc(series.id)}">
    <span class="row-icon tone-accent">${icon('bell')}</span>
    <span class="row-main"><strong>${esc(series.title)}</strong><small>${esc(ruleLabel(series.recurrence_rule))} · ${esc(String(series.dtstart_local || '').slice(11, 16))}${series.status === 'ENDED' ? ` · ${esc(t('checkin.ended'))}` : next ? ` · ${esc(t('checkin.next', { when: fmtDateTime(next) }))}` : ''}${series._pending ? ` · ${esc(t('checkin.pendingSync'))}` : ''}</small></span>
    ${series.status === 'ACTIVE' ? `<button class="button small ghost" data-action="series-menu" data-id="${esc(series.id)}" aria-label="${esc(t('checkin.more', { title: series.title }))}">${esc(t('capture.more'))}</button>` : ''}
  </div>`;
}

export async function seriesMenu(series) {
  const choice = await actionSheet({ title: series.title, items: [
    { id: 'rename', icon: 'note', label: t('checkin.rename') },
    { id: 'end', icon: 'x', label: t('checkin.endSeries') },
    { id: 'delete', icon: 'x', label: t('lifecycle.delete'), tone: 'danger' },
  ] });
  if (choice === 'rename') {
    const dialog = openSheet({ title: t('checkin.rename'), body: `<label class="field"><span>${esc(t('form.title'))}</span><input data-rename maxlength="300" value="${esc(series.title)}"></label>`,
      actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>` });
    dialog.querySelector('[data-save]').addEventListener('click', async () => {
      const title = dialog.querySelector('[data-rename]').value.trim();
      dialog.close('saved');
      if (title && title !== series.title) await change('reminder_series.update', series.id, { title }, { success: t('checkin.toast.updated') });
    });
  } else if (choice === 'end') {
    await change('reminder_series.end', series.id, {}, { success: t('checkin.toast.ended') });
  } else if (choice === 'delete') {
    const ok = await confirmSheet({ title: t('checkin.deleteSeriesTitle'), body: `<p>${esc(t('checkin.deleteSeriesBody', { title: series.title }))}</p>`,
      confirmLabel: t('lifecycle.deleteConfirm'), cancelLabel: t('common.keep'), danger: true });
    if (ok) await change('reminder_series.delete', series.id, {}, { success: t('checkin.toast.deleted') });
  }
}

export const fmtPace = (seconds) => (seconds ? fmtDuration(Math.max(1, Math.round(seconds / 60))) : '');
export const localeIsRu = () => getLocale() === 'ru';
