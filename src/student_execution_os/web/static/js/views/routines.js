import { load } from '../store.js';
import { t, fmtDateTime, fmtDuration, fmtRelative } from '../i18n.js';
import { esc, icon, chip, empty, openSheet, chipGroup, chipValue, localInputValue, setBusy } from '../ui.js';
import { change, shell } from '../actions.js';
import { newEntityId } from '../sync.js';

function localNowInput(days = 1) {
  const value = new Date(Date.now() + days * 86400000);
  value.setSeconds(0, 0);
  return localInputValue(value);
}

function ruleLabel(rule) {
  const parts = Object.fromEntries(String(rule || '').split(';').map((part) => part.split('=')));
  if (parts.FREQ === 'DAILY') return parts.INTERVAL === '2' ? t('routine.every2Days') : t('routine.daily');
  return parts.INTERVAL === '2' ? t('routine.biweekly') : t('routine.weekly');
}

function recurrenceRule(value) {
  if (value === 'DAILY') return 'FREQ=DAILY';
  if (value === 'DAILY2') return 'FREQ=DAILY;INTERVAL=2';
  if (value === 'WEEKLY2') return 'FREQ=WEEKLY;INTERVAL=2';
  return 'FREQ=WEEKLY';
}

function createRoutine() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const dialog = openSheet({
    title: t('routine.new'),
    body: `<label class="field"><span>${esc(t('form.title'))}</span>
        <input data-routine-title maxlength="300" autocomplete="off">
      </label>
      <label class="field"><span>${esc(t('form.effort'))}</span>
        <input type="number" min="1" max="100000" value="45" data-routine-effort>
      </label>
      <label class="field"><span>${esc(t('routine.firstTarget'))}</span>
        <input type="datetime-local" value="${esc(localNowInput(1))}" data-routine-start>
      </label>
      <div class="field"><span>${esc(t('routine.repeat'))}</span>
        ${chipGroup('routine-frequency', [
          ['DAILY', t('routine.daily')],
          ['WEEKLY', t('routine.weekly')],
          ['WEEKLY2', t('routine.biweekly')],
        ], 'WEEKLY')}
      </div>
      <p class="help">${esc(t('routine.timezone', { zone }))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-routine-create>${esc(t('compose.create'))}</button>`,
  });
  const title = dialog.querySelector('[data-routine-title]');
  title?.focus();
  dialog.querySelector('[data-routine-create]').addEventListener('click', async (event) => {
    const name = String(title.value || '').trim();
    const effort = Number(dialog.querySelector('[data-routine-effort]').value || 0);
    const start = String(dialog.querySelector('[data-routine-start]').value || '');
    if (!name || effort <= 0 || !start) return;
    setBusy(event.currentTarget, true);
    const result = await change('routine.create', newEntityId('routine'), {
      title: name,
      dtstart_local: start,
      effort_minutes: effort,
      recurrence_rule: recurrenceRule(chipValue(dialog, 'routine-frequency')),
      timezone_name: zone,
      splittable: effort > 30,
      min_chunk_minutes: effort > 30 ? 15 : null,
      max_chunk_minutes: effort > 30 ? Math.min(90, effort) : null,
    }, { success: t('routine.created') });
    if (result) dialog.close('created'); else setBusy(event.currentTarget, false);
  });
}

function editOccurrence(routine, occurrence) {
  const currentLocal = occurrence.override_target_local || occurrence.original_recurrence_id;
  const dialog = openSheet({
    eyebrow: routine.title,
    title: t('routine.editOccurrence'),
    body: `<label class="field"><span>${esc(t('form.title'))}</span>
        <input data-occurrence-title maxlength="300" value="${esc(occurrence.title || routine.title)}">
      </label>
      <label class="field"><span>${esc(t('form.effort'))}</span>
        <input type="number" min="1" max="100000" data-occurrence-effort value="${Number(occurrence.effort_minutes || routine.effort_minutes)}">
      </label>
      <label class="field"><span>${esc(t('routine.targetLocal'))}</span>
        <input type="datetime-local" data-occurrence-target value="${esc(currentLocal.slice(0, 16))}">
      </label>
      <p class="help">${esc(t('routine.identityHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-occurrence-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-occurrence-save]').addEventListener('click', async (event) => {
    const title = String(dialog.querySelector('[data-occurrence-title]').value || '').trim();
    const effort = Number(dialog.querySelector('[data-occurrence-effort]').value || 0);
    const target = String(dialog.querySelector('[data-occurrence-target]').value || '');
    if (!title || effort <= 0 || !target) return;
    setBusy(event.currentTarget, true);
    const result = await change('routine.occurrence.edit', occurrence.task_id, {
      template_id: routine.id,
      original_recurrence_id: occurrence.original_recurrence_id,
      task_id: occurrence.task_id,
      title,
      effort_minutes: effort,
      target_local: target,
    }, { success: t('routine.occurrenceUpdated') });
    if (result) dialog.close('saved'); else setBusy(event.currentTarget, false);
  });
}

function occurrenceRow(routine, occurrence) {
  const skipped = occurrence.state === 'SKIPPED';
  const closed = ['COMPLETED', 'ARCHIVED'].includes(occurrence.task_status);
  return `<div class="row" data-id="${esc(occurrence.task_id)}">
    <span class="row-time"><strong>${esc(fmtDateTime(occurrence.target_at))}</strong>
      <small>${esc(fmtRelative(occurrence.target_at))}</small></span>
    <button class="row-main" data-action="routine-open-task" data-id="${esc(occurrence.task_id)}">
      <strong>${esc(occurrence.title || routine.title)}</strong>
      <small>${esc(t('routine.remaining', { d: fmtDuration(occurrence.remaining_effort_minutes || occurrence.effort_minutes || 0) }))}</small>
    </button>
    ${skipped ? chip(t('routine.skipped'), 'muted') : closed ? chip(t('routine.done'), 'ok') : occurrence._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
    ${closed ? '' : `<button class="icon-button" data-action="routine-edit-occurrence" data-routine="${esc(routine.id)}" data-original="${esc(occurrence.original_recurrence_id)}" aria-label="${esc(t('routine.editOccurrence'))}">${icon('settings')}</button>
      <button class="button ghost" data-action="${skipped ? 'routine-reopen-occurrence' : 'routine-skip-occurrence'}"
        data-routine="${esc(routine.id)}" data-original="${esc(occurrence.original_recurrence_id)}" data-task="${esc(occurrence.task_id)}">
        ${esc(skipped ? t('routine.reopenOccurrence') : t('routine.skipOccurrence'))}</button>`}
  </div>`;
}

function routineCard(routine) {
  const active = routine.status === 'ACTIVE';
  const occurrences = (routine.occurrences || []).filter((o) => new Date(o.target_at) >= new Date(Date.now() - 7 * 86400000));
  return `<section class="card" data-id="${esc(routine.id)}">
    <div class="section-head">
      <div><h3>${esc(routine.title)}</h3>
        <p>${esc(ruleLabel(routine.recurrence_rule))} · ${esc(fmtDuration(routine.effort_minutes))} · ${esc(routine.timezone_name)}</p></div>
      ${active ? `<button class="button ghost" data-action="routine-cancel" data-id="${esc(routine.id)}" data-version="${Number(routine.version)}">${esc(t('routine.stop'))}</button>`
        : chip(t('routine.stopped'), 'muted')}
    </div>
    ${occurrences.length ? `<div class="list">${occurrences.slice(0, 10).map((o) => occurrenceRow(routine, o)).join('')}</div>`
      : `<p class="muted">${esc(routine._pending ? t('routine.pendingMaterialization') : t('routine.noOccurrences'))}</p>`}
  </section>`;
}

export default {
  id: 'routines',
  tab: 'more',
  title: () => t('nav.routines'),
  async load({ fresh }) {
    return load('/api/v1/work-routines', { fresh });
  },
  render(data) {
    this._data = data;
    const routines = data.routines || [];
    return `<section class="section">
      <div class="section-head"><div><h2>${esc(t('routine.title'))}</h2><p>${esc(t('routine.help'))}</p></div>
        <button class="button primary" data-action="routine-create">${icon('plus')}${esc(t('routine.new'))}</button></div>
      ${routines.length ? `<div class="stack">${routines.map(routineCard).join('')}</div>`
        : empty(t('routine.empty'), t('routine.emptyHelp'), 'calendar')}
    </section>`;
  },
  actions: {
    'routine-create'() { createRoutine(); },
    async 'routine-cancel'(el) {
      await change('routine.cancel', el.dataset.id, { expected_version: Number(el.dataset.version) }, { success: t('routine.stopped') });
    },
    'routine-open-task'(el) { shell.go('task', { params: [el.dataset.id] }); },
    'routine-edit-occurrence'(el, ctx) {
      const routine = (ctx.data.routines || []).find((x) => x.id === el.dataset.routine);
      const occurrence = routine?.occurrences?.find((x) => x.original_recurrence_id === el.dataset.original);
      if (routine && occurrence) editOccurrence(routine, occurrence);
    },
    async 'routine-skip-occurrence'(el) {
      await change('routine.occurrence.skip', el.dataset.task, {
        template_id: el.dataset.routine,
        original_recurrence_id: el.dataset.original,
        task_id: el.dataset.task,
      }, { success: t('routine.occurrenceSkipped') });
    },
    async 'routine-reopen-occurrence'(el) {
      await change('routine.occurrence.reopen', el.dataset.task, {
        template_id: el.dataset.routine,
        original_recurrence_id: el.dataset.original,
        task_id: el.dataset.task,
      }, { success: t('routine.occurrenceReopened') });
    },
  },
};
