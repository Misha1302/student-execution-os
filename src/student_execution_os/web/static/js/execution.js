import { t, fmtDuration } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, toast, localInputValue, isoFromLocalInput } from './ui.js';
import { change } from './actions.js';
import { newEntityId } from './sync.js';
import { showExecutionNotification, clearExecutionNotification } from './native.js';

export function executionSeconds(session, at = Date.now()) {
  if (!session) return 0;
  const measuredSeconds = Math.max(0, Number(session.actual_work_seconds || 0));
  if (session.state !== 'ACTIVE' || !session.current_segment_started_at) return measuredSeconds;
  const segmentStart = new Date(session.current_segment_started_at).getTime();
  const measuredAt = new Date(session.measured_at || session.current_segment_started_at).getTime();
  const measuredCurrentSegment = Math.max(0, Math.floor((measuredAt - segmentStart) / 1000));
  const closedSegments = Math.max(0, measuredSeconds - measuredCurrentSegment);
  const currentAtTarget = Math.max(0, Math.floor((at - segmentStart) / 1000));
  return closedSegments + currentAtTarget;
}

export function clockText(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((x) => String(x).padStart(2, '0')).join(':');
}

export function executionCard(session, task) {
  if (!session) return '';
  const paused = session.state === 'PAUSED';
  const suspicious = executionSeconds(session) >= 8 * 3600;
  return `<article class="card now-card execution-card" data-execution-id="${esc(session.id)}">
    <div class="now-head">
      <span class="eyebrow">${esc(paused ? t('execution.paused') : t('execution.active'))}</span>
      <span class="chip accent">${icon('clock')}<span data-execution-timer="${esc(session.id)}">${clockText(executionSeconds(session))}</span></span>
    </div>
    <h3>${esc(task?.title || session.task_title || t('execution.work'))}</h3>
    <p class="muted">${esc(t('execution.startedAt', { when: new Date(session.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }))}</p>
    ${suspicious ? `<div class="banner warn"><div><strong>${esc(t('execution.longRunning'))}</strong><p>${esc(t('execution.longRunningHelp'))}</p></div>
      <button class="button small" data-action="execution-review" data-id="${esc(session.id)}">${esc(t('execution.reviewTimer'))}</button></div>` : ''}
    <div class="now-actions">
      ${paused
        ? `<button class="button primary" data-action="execution-resume" data-id="${esc(session.id)}">${esc(t('execution.resume'))}</button>`
        : `<button class="button" data-action="execution-pause" data-id="${esc(session.id)}">${esc(t('execution.pause'))}</button>`}
      <button class="button primary" data-action="execution-finish" data-id="${esc(session.id)}">${esc(t('execution.finish'))}</button>
      <button class="button ghost" data-action="execution-complete" data-id="${esc(session.id)}">${esc(t('execution.taskDone'))}</button>
    </div>
  </article>`;
}

export function mountExecutionTimers(root, session, task = null) {
  if (!session) {
    clearExecutionNotification().catch(() => {});
    return () => {};
  }
  showExecutionNotification(session, task?.title || session.task_title).catch(() => {});
  const render = () => {
    root.querySelectorAll(`[data-execution-timer="${CSS.escape(session.id)}"]`).forEach((el) => {
      el.textContent = clockText(executionSeconds(session));
    });
  };
  render();
  const handle = setInterval(() => {
    if (!root.isConnected) { clearInterval(handle); return; }
    render();
  }, 1000);
  return () => clearInterval(handle);
}

export async function startExecution(task, plan) {
  if (!task) return null;
  const block = (plan?.blocks || []).find((b) => b.type === 'WORK' && b.obligation_id === task.id);
  const id = newEntityId('execution');
  const occurredAt = new Date().toISOString();
  const result = await change('execution.start', id, {
    task_id: task.id,
    planning_snapshot_id: plan?.id || null,
    source_plan_block_id: block?.id || null,
    occurred_at: occurredAt,
  }, { success: t('execution.started') });
  if (result) showExecutionNotification({
    id, task_id: task.id, task_title: task.title, state: 'ACTIVE',
    started_at: occurredAt, measured_at: occurredAt, current_segment_started_at: occurredAt, actual_work_seconds: 0,
  }, task.title).catch(() => {});
  return result;
}

export async function pauseExecution(session) {
  const occurredAt = new Date().toISOString();
  const result = await change('execution.pause', session.id, { occurred_at: occurredAt }, { success: t('execution.pausedToast') });
  if (result) showExecutionNotification({
    ...session, state: 'PAUSED', measured_at: occurredAt, current_segment_started_at: null,
    actual_work_seconds: executionSeconds(session), updated_at: occurredAt,
  }, session.task_title).catch(() => {});
  return result;
}

export async function resumeExecution(session) {
  const occurredAt = new Date().toISOString();
  const result = await change('execution.resume', session.id, { occurred_at: occurredAt }, { success: t('execution.resumedToast') });
  if (result) showExecutionNotification({
    ...session, state: 'ACTIVE', measured_at: occurredAt, current_segment_started_at: occurredAt, updated_at: occurredAt,
  }, session.task_title).catch(() => {});
  return result;
}

export async function finishExecution(session, task, { complete = false, occurredAt = null } = {}) {
  if (!session || !task) return Promise.resolve(null);
  const finishAt = occurredAt || new Date().toISOString();
  if (complete) {
    const result = await change('execution.finish', session.id, { task_id: task.id, outcome: 'COMPLETE', occurred_at: finishAt }, { success: t('execution.completed') });
    if (result) clearExecutionNotification().catch(() => {});
    return result;
  }
  const worked = Math.max(1, Math.round(executionSeconds(session, new Date(finishAt).getTime()) / 60));
  const current = Number(task.remaining_effort_minutes || 0);
  const suggested = Math.max(0, current - worked);
  const options = [...new Set([suggested, 15, 30, 45, 60, 90, current].filter((x) => Number.isFinite(x) && x >= 0))]
    .sort((a, b) => a - b)
    .map((m) => [String(m), m === 0 ? t('execution.doneOption') : fmtDuration(m)]);
  return new Promise((resolve) => {
    const dialog = openSheet({
      eyebrow: task.title,
      title: t('execution.finishTitle'),
      body: `<p class="muted">${esc(t('execution.worked', { d: fmtDuration(worked) }))}</p>
        <div class="field"><span>${esc(t('execution.whatChanged'))}</span>
          ${chipGroup('execution-outcome', [
            ['UPDATE_REMAINING', t('execution.updateRemaining')],
            ['KEEP_REMAINING', t('execution.keepRemaining')],
            ['COMPLETE', t('execution.doneOption')],
          ], 'UPDATE_REMAINING')}</div>
        <div class="field" data-execution-remaining><span>${esc(t('execution.remaining'))}</span>
          ${chipGroup('execution-remaining', options, String(suggested))}</div>
        <p class="help">${esc(t('execution.noAutoProgress'))}</p>`,
      actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
        <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
      onClose: (value) => { if (value !== 'saved') resolve(null); },
    });
    const remaining = dialog.querySelector('[data-execution-remaining]');
    const syncVisibility = () => {
      remaining.hidden = chipValue(dialog, 'execution-outcome') !== 'UPDATE_REMAINING';
    };
    dialog.addEventListener('chipchange', (event) => {
      if (event.detail.name === 'execution-outcome') syncVisibility();
    });
    syncVisibility();
    dialog.querySelector('[data-save]').addEventListener('click', async () => {
      const outcome = chipValue(dialog, 'execution-outcome') || 'KEEP_REMAINING';
      const payload = { task_id: task.id, outcome, occurred_at: finishAt };
      if (outcome === 'UPDATE_REMAINING') payload.remaining_effort_minutes = Number(chipValue(dialog, 'execution-remaining') || 0);
      const result = await change('execution.finish', session.id, payload, { success: t('execution.saved') });
      if (result) clearExecutionNotification().catch(() => {});
      dialog.close('saved');
      if (result) toast(t('execution.replanning'));
      resolve(result);
    });
  });
}

export function reviewLongExecution(session, task) {
  if (!session || !task) return;
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('execution.reviewTimer'),
    body: `<p class="muted">${esc(t('execution.longRunningHelp'))}</p>
      <label class="field"><span>${esc(t('execution.finishedAt'))}</span>
        <input type="datetime-local" data-execution-finished-at value="${esc(localInputValue(new Date()))}">
      </label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('execution.stillWorking'))}</button>
      <button type="button" class="button primary" data-finish-at>${esc(t('execution.finishAtTime'))}</button>`,
  });
  dialog.querySelector('[data-finish-at]').addEventListener('click', async () => {
    const raw = dialog.querySelector('[data-execution-finished-at]').value;
    const instant = isoFromLocalInput(raw);
    if (!instant || new Date(instant) < new Date(session.started_at) || new Date(instant) > new Date()) {
      toast(t('execution.invalidFinishTime'), { error: true });
      return;
    }
    dialog.close('correct');
    await finishExecution(session, task, { occurredAt: instant });
  });
}

