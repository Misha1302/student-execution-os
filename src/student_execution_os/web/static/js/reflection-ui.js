import { change } from './actions.js';
import { t, fmtDuration } from './i18n.js';
import { esc, icon, openSheet, setBusy, toast } from './ui.js';

export function dailyIntentCard(data) {
  const intent = data?.daily_intent || null;
  const taskById = new Map((data?.tasks || []).map((task) => [task.id, task]));
  const chosen = (intent?.priority_task_ids || []).map((id) => taskById.get(id)).filter(Boolean);
  const capacity = data?.day_capacity || {};
  const metrics = capacity.planning_capacity_minutes == null ? '' : `
    <div class="mini-kv">
      <div><dt>${esc(t('intent.capacity'))}</dt><dd>${esc(fmtDuration(capacity.planning_capacity_minutes))}</dd></div>
      <div><dt>${esc(t('intent.planned'))}</dt><dd>${esc(fmtDuration(capacity.planned_work_minutes || 0))}</dd></div>
      <div><dt>${esc(t('intent.reserve'))}</dt><dd>${esc(fmtDuration(capacity.safe_reserve_minutes || 0))}</dd></div>
    </div>`;
  if (!intent) {
    return `<article class="card">
      <div class="section-head"><div><span class="eyebrow">${esc(t('intent.eyebrow'))}</span>
        <h3>${esc(t('intent.notSet'))}</h3></div>
        <button class="button primary" data-action="intent-edit">${icon('plan')}${esc(t('intent.planDay'))}</button></div>
      ${metrics}<p class="help">${esc(t('intent.help'))}</p>
    </article>`;
  }
  return `<article class="card">
    <div class="section-head"><div><span class="eyebrow">${esc(intent.closed_at ? t('intent.closed') : t('intent.eyebrow'))}</span>
      <h3>${esc(t('intent.todayPriorities'))}</h3></div>
      <button class="button ghost" data-action="intent-edit">${esc(t('common.edit'))}</button></div>
    ${chosen.length ? `<div class="list">${chosen.map((task, index) => `<button class="row" data-action="open-task" data-id="${esc(task.id)}">
      <span class="count">${index + 1}</span><span class="row-main"><strong>${esc(task.title)}</strong></span>
    </button>`).join('')}</div>` : `<p class="muted">${esc(t('intent.noPriorities'))}</p>`}
    ${intent.note ? `<p>${esc(intent.note)}</p>` : ''}
    ${metrics}
    ${data?.plan?.pending_intent ? `<p class="help">${esc(t('intent.pending'))}</p>` : ''}
  </article>`;
}

export function openDailyIntentSheet({ localDate, intent, tasks }) {
  const openTasks = (tasks || []).filter((task) => ['ACTIVE', 'DRAFT'].includes(task.status));
  const selected = new Set(intent?.priority_task_ids || []);
  const body = openTasks.length ? `<p class="help">${esc(t('intent.chooseHelp'))}</p>
    <div class="list">${openTasks.map((task) => `<label class="row">
      <input type="checkbox" data-intent-task value="${esc(task.id)}" ${selected.has(task.id) ? 'checked' : ''}>
      <span class="row-main"><strong>${esc(task.title)}</strong>
        <small>${task.remaining_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(fmtDuration(task.remaining_effort_minutes))}</small></span>
    </label>`).join('')}</div>`
    : `<p class="muted">${esc(t('intent.noOpenTasks'))}</p>`;
  const dialog = openSheet({
    eyebrow: localDate,
    title: t('intent.planDay'),
    body: `${body}
      <label class="field"><span>${esc(t('intent.note'))}</span>
        <textarea rows="3" maxlength="2000" data-intent-note>${esc(intent?.note || '')}</textarea>
      </label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-intent-save>${esc(t('common.save'))}</button>`,
  });
  dialog.addEventListener('change', (event) => {
    if (!event.target.matches('[data-intent-task]')) return;
    const checked = [...dialog.querySelectorAll('[data-intent-task]:checked')];
    if (checked.length > 3) {
      event.target.checked = false;
      toast(t('intent.maxThree'), { error: true });
    }
  });
  dialog.querySelector('[data-intent-save]').addEventListener('click', async (event) => {
    setBusy(event.currentTarget, true);
    const priority = [...dialog.querySelectorAll('[data-intent-task]:checked')].map((el) => el.value).slice(0, 3);
    const note = String(dialog.querySelector('[data-intent-note]').value || '').trim() || null;
    const result = await change('intent.set', `intent-${localDate}`, {
      local_date: localDate,
      priority_task_ids: priority,
      note,
      expected_version: intent?.version || 0,
    }, { success: t('intent.saved') });
    if (result) dialog.close('saved'); else setBusy(event.currentTarget, false);
  });
}

export async function closeDailyIntent(localDate, intent) {
  return change('intent.close', `intent-${localDate}`, {
    local_date: localDate,
    expected_version: intent?.version || 0,
  }, { success: t('intent.dayClosed') });
}

export async function applyCalibration(row, mode) {
  const current = row.preference || null;
  let multiplier = Number(current?.safety_multiplier || 1);
  let enabled = Boolean(current?.enabled);
  let suppress = Boolean(current?.suppress_suggestion);
  if (mode === 'accept') {
    multiplier = Number(row.suggested_multiplier || 1);
    enabled = true;
    suppress = false;
  } else if (mode === 'disable') {
    enabled = false;
    suppress = false;
  } else if (mode === 'suppress') {
    enabled = false;
    suppress = true;
  }
  return change('calibration.set', `calibration-${row.category}`, {
    category: row.category,
    safety_multiplier: multiplier,
    enabled,
    suppress_suggestion: suppress,
    expected_version: current?.version ?? 0,
  }, { success: t('reflection.calibrationSaved') });
}
