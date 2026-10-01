// Editing and rescheduling an existing task (not capture): each save is one
// offline-safe queued operation.
import { openSheet, esc, focusSoon, toast, localInputValue, isoFromLocalInput } from './ui.js';
import { t, now, fmtDateTime } from './i18n.js';
import { fieldsHtml, bindFields, readFields, deadlineText, endOfDay, at } from './task-draft.js';
import { focusDurationOther } from './duration.js';
import { change } from './actions.js';

function changedFields(task, fields) {
  const changes = {};
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const instant = (v) => (v ? new Date(v).getTime() : null);
  for (const key of ['title', 'description', 'category', 'importance', 'estimated_total_effort_minutes', 'splittable']) {
    if (!same(fields[key], task[key])) changes[key] = fields[key];
  }
  if (fields.splittable) {
    for (const key of ['min_chunk_minutes', 'max_chunk_minutes']) if (!same(fields[key], task[key])) changes[key] = fields[key];
  }
  for (const key of ['actionable_from', 'target_at', 'remind_at']) {
    if (instant(fields[key]) !== instant(task[key])) changes[key] = fields[key];
  }
  const a = fields.actual_cutoff; const b = task.actual_cutoff || {};
  if (a.state !== b.state || (a.state === 'KNOWN' && instant(a.at) !== instant(b.at))) changes.actual_cutoff = a;
  const count = task.count_progress || null;
  if ((fields.count_total ?? null) !== (count?.total ?? null)) { changes.count_total = fields.count_total; changes.count_unit = fields.count_unit; }
  else if (fields.count_total && (fields.count_unit ?? null) !== (count?.unit ?? null)) changes.count_unit = fields.count_unit;
  if ('remaining_effort_minutes' in fields && fields.remaining_effort_minutes !== task.remaining_effort_minutes
    && fields.remaining_effort_minutes != null) changes.remaining_effort_minutes = fields.remaining_effort_minutes;
  return changes;
}

export function editTaskSheet(task, { focus } = {}) {
  const started = task.remaining_effort_minutes != null && task.remaining_effort_minutes !== task.estimated_total_effort_minutes;
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('task.edit'),
    full: true,
    body: fieldsHtml(task, { remaining: started }),
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  bindFields(dialog);
  if (focus === 'effort') setTimeout(() => focusDurationOther(dialog, 'f-effort'), 80);
  else if (focus) focusSoon(() => dialog.querySelector(`[data-field="${focus}"]`)?.querySelector('input,select,button'));
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    let fields;
    try { fields = readFields(dialog); } catch (err) { toast(err.message, { error: true }); return; }
    if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
    if (fields.remind_at && new Date(fields.remind_at) <= now()) { toast(t('form.remindPast'), { error: true }); return; }
    const changes = changedFields(task, fields);
    if (!Object.keys(changes).length) { dialog.close('unchanged'); return; }
    const saved = await change('task.update', task.id, changes, { success: t('task.saved') });
    if (saved) dialog.close('saved');
  });
}

function shiftDays(iso, days) {
  const d = new Date(iso);
  d.setDate(d.getDate() + days);
  while (d <= now()) d.setDate(d.getDate() + 1);
  return d.toISOString();
}

// "Перенести": move the deadline or put the task off — each tap is one offline-safe operation.
export function rescheduleSheet(task) {
  const cutoff = task.actual_cutoff || { state: 'UNKNOWN' };
  const known = cutoff.state === 'KNOWN';
  const deadlineOptions = known
    ? [['d1', t('resched.plusDay')], ['d3', t('resched.plus3')], ['d7', t('resched.plusWeek')], ['pick', t('form.deadline.exact')], ['none', t('resched.noDeadline')]]
    : [['today', t('resched.tonight')], ['tomorrow', t('day.tomorrow')], ['week', t('form.deadline.week')], ['pick', t('form.deadline.exact')]];
  const later = [['h1', t('resched.inHour')], ...(now().getHours() < 18 ? [['evening', t('resched.evening')]] : []),
    ['morning', t('resched.tomorrowMorning')], ['pick', t('form.deadline.exact')]];
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('resched.title'),
    body: `<section class="field"><span>${esc(t('resched.deadline'))}</span>
        <p class="muted">${esc(t('resched.current', { when: deadlineText(cutoff) }))}</p>
        <div class="chip-row">${deadlineOptions.map(([v, label]) => `<button type="button" class="chip-toggle" data-deadline="${v}">${esc(label)}</button>`).join('')}</div>
        <div class="field-row hidden" data-pick-deadline><input type="datetime-local" data-f="deadline" value="${esc(localInputValue(known ? cutoff.at : endOfDay(1)))}">
          <button type="button" class="button primary" data-save-deadline>${esc(t('common.save'))}</button></div>
      </section>
      ${task.status === 'ACTIVE' || task.status === 'DRAFT' ? `<section class="field"><span>${esc(t('resched.later'))}</span>
        <p class="help">${esc(t('resched.laterHelp'))}</p>
        <div class="chip-row">${later.map(([v, label]) => `<button type="button" class="chip-toggle" data-later="${v}">${esc(label)}</button>`).join('')}</div>
        <div class="field-row hidden" data-pick-later><input type="datetime-local" data-f="later" value="${esc(localInputValue(at(1, 9)))}">
          <button type="button" class="button primary" data-save-later>${esc(t('common.save'))}</button></div>
      </section>` : ''}`,
  });
  const update = async (changes, success) => {
    const saved = await change('task.update', task.id, changes, { success });
    if (saved) dialog.close('saved');
  };
  const defer = async (until) => {
    if (new Date(until) <= now()) { toast(t('resched.past'), { error: true }); return; }
    const saved = await change('task.defer', task.id, { until }, { success: t('resched.deferred', { when: fmtDateTime(until) }) });
    if (saved) dialog.close('saved');
  };
  dialog.addEventListener('click', (e) => {
    const d = e.target.closest('[data-deadline]');
    if (d) {
      const v = d.dataset.deadline;
      if (v === 'pick') { dialog.querySelector('[data-pick-deadline]').classList.remove('hidden'); return; }
      let at_;
      if (v === 'none') { update({ actual_cutoff: { state: 'ABSENT' } }, t('resched.moved')); return; }
      if (v.startsWith('d')) at_ = shiftDays(cutoff.at, Number(v.slice(1)));
      else at_ = (v === 'today' ? endOfDay(0) : v === 'tomorrow' ? endOfDay(1) : endOfDay(7)).toISOString();
      update({ actual_cutoff: { state: 'KNOWN', at: at_ } }, t('resched.movedTo', { when: fmtDateTime(at_) }));
      return;
    }
    const l = e.target.closest('[data-later]');
    if (l) {
      const v = l.dataset.later;
      if (v === 'pick') { dialog.querySelector('[data-pick-later]').classList.remove('hidden'); return; }
      const until = v === 'h1' ? new Date(now().getTime() + 3600000) : v === 'evening' ? at(0, 19) : at(1, 9);
      defer(new Date(Math.ceil(until.getTime() / 60000) * 60000).toISOString());
    }
  });
  dialog.querySelector('[data-save-deadline]')?.addEventListener('click', () => {
    const value = isoFromLocalInput(dialog.querySelector('[data-f="deadline"]').value);
    if (!value) { toast(t('form.deadlineRequired'), { error: true }); return; }
    update({ actual_cutoff: { state: 'KNOWN', at: value } }, t('resched.movedTo', { when: fmtDateTime(value) }));
  });
  dialog.querySelector('[data-save-later]')?.addEventListener('click', () => {
    const value = isoFromLocalInput(dialog.querySelector('[data-f="later"]').value);
    if (value) defer(value);
  });
  return dialog;
}
