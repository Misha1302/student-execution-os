import { t, fmtDuration } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, toast } from './ui.js';
import { change } from './actions.js';
import { newEntityId } from './sync.js';

export function executionSeconds(session, at = Date.now()) {
  if (!session) return 0;
  let seconds = Number(session.actual_work_seconds || 0);
  if (session.state === 'ACTIVE' && session.current_segment_started_at) {
    seconds += Math.max(0, Math.floor((at - new Date(session.current_segment_started_at).getTime()) / 1000));
  }
  return Math.max(0, seconds);
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
  return `<article class="card now-card execution-card" data-execution-id="${esc(session.id)}">
    <div class="now-head">
      <span class="eyebrow">${esc(paused ? t('execution.paused') : t('execution.active'))}</span>
      <span class="chip accent">${icon('clock')}<span data-execution-timer="${esc(session.id)}">${clockText(executionSeconds(session))}</span></span>
    </div>
    <h3>${esc(task?.title || session.task_title || t('execution.work'))}</h3>
    <p class="muted">${esc(t('execution.startedAt', { when: new Date(session.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }))}</p>
    <div class="now-actions">
      ${paused
        ? `<button class="button primary" data-action="execution-resume" data-id="${esc(session.id)}">${esc(t('execution.resume'))}</button>`
        : `<button class="button" data-action="execution-pause" data-id="${esc(session.id)}">${esc(t('execution.pause'))}</button>`}
      <button class="button primary" data-action="execution-finish" data-id="${esc(session.id)}">${esc(t('execution.finish'))}</button>
      <button class="button ghost" data-action="execution-complete" data-id="${esc(session.id)}">${esc(t('execution.taskDone'))}</button>
    </div>
  </article>`;
}

export function mountExecutionTimers(root, session) {
  if (!session) return () => {};
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
  return change('execution.start', newEntityId('execution'), {
    task_id: task.id,
    planning_snapshot_id: plan?.id || null,
    source_plan_block_id: block?.id || null,
  }, { success: t('execution.started') });
}

export function pauseExecution(session) {
  return change('execution.pause', session.id, {}, { success: t('execution.pausedToast') });
}

export function resumeExecution(session) {
  return change('execution.resume', session.id, {}, { success: t('execution.resumedToast') });
}

export function finishExecution(session, task, { complete = false } = {}) {
  if (!session || !task) return Promise.resolve(null);
  if (complete) {
    return change('execution.finish', session.id, { task_id: task.id, outcome: 'COMPLETE' }, { success: t('execution.completed') });
  }
  const worked = Math.max(1, Math.round(executionSeconds(session) / 60));
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
      const payload = { task_id: task.id, outcome };
      if (outcome === 'UPDATE_REMAINING') payload.remaining_effort_minutes = Number(chipValue(dialog, 'execution-remaining') || 0);
      const result = await change('execution.finish', session.id, payload, { success: t('execution.saved') });
      dialog.close('saved');
      if (result) toast(t('execution.replanning'));
      resolve(result);
    });
  });
}
