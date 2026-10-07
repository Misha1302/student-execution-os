import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, riskChip, kv, empty, setBusy } from '../ui.js';
import { DURATION_PRESETS } from '../duration.js';
import { lifecycle, logProgress, mutate, change, taskPlace } from '../actions.js';
import { editTaskSheet, rescheduleSheet } from '../task-sheets.js';
import { deadlineText } from '../task-draft.js';
import { checklistSummary } from '../subtask-overlay.js';
import { openSheet, actionSheet } from '../ui.js';
import { newEntityId } from '../sync.js';
import { executionCard, mountExecutionTimers, startExecution, pauseExecution, resumeExecution, finishExecution, reviewLongExecution } from '../execution.js';

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.readAsDataURL(file);
  });
}

function when(value) {
  return value ? `${fmtDateTime(value)} · ${fmtRelative(value)}` : '—';
}

// "Лучше начать не позже 16:20" instead of the planner's latest_safe_start.
export function startAdvice(task) {
  const lss = task.risk?.latest_safe_start;
  if (!lss || task.started_at) return null;
  const at = new Date(lss);
  return at < now() ? t('task.startNowAdvice') : t('task.startBy', { when: fmtDateTime(at) });
}

// The task's steps. Their effort is a breakdown next to the task's estimate; only the
// task's own remaining effort is planned (nothing is counted twice).
function checklistCard(task, total) {
  const items = task.subtasks || [];
  const summary = checklistSummary(items);
  const stepEffort = summary?.effort_minutes;
  const rows = items.map((x, i) => `<div class="row static subtask-row${x.done ? ' done' : ''}">
    <label class="subtask-check"><input type="checkbox" data-subtask-toggle data-id="${esc(x.id)}" data-task-id="${esc(x.task_id)}" ${x.done ? 'checked' : ''}
      aria-label="${esc(t(x.done ? 'subtask.markOpen' : 'subtask.markDone', { title: x.title }))}"><span>${esc(x.title)}</span></label>
    <span class="row-aside">${x.effort_minutes ? `<small class="muted">${esc(fmtDuration(x.effort_minutes))}</small>` : ''}${x._pending ? `<small class="muted">${esc(t('checkin.pendingSync'))}</small>` : ''}
      <button class="button small ghost" data-action="subtask-menu" data-id="${esc(x.id)}" data-index="${i}" aria-label="${esc(t('checkin.more', { title: x.title }))}">⋯</button></span>
  </div>`).join('');
  return `<section class="card" data-checklist>
    <div class="section-head"><h3>${esc(t('subtask.title'))}</h3>${summary ? `<span class="muted">${esc(t('subtask.progress', { done: summary.done, total: summary.total }))}</span>` : ''}</div>
    ${rows ? `<div class="list">${rows}</div>` : `<p class="muted">${esc(t('subtask.empty'))}</p>`}
    ${stepEffort && total && stepEffort !== total ? `<p class="help">${esc(t('subtask.effortMismatch', { steps: fmtDuration(stepEffort), task: fmtDuration(total) }))}</p>` : ''}
    <form class="inline-add" data-subtask-form data-task-id="${esc(task.id)}"><input data-subtask-title maxlength="300" value="${esc(stepDraft.taskId === task.id ? stepDraft.text : '')}" placeholder="${esc(t('subtask.addPlaceholder'))}" aria-label="${esc(t('subtask.add'))}">
      <button class="button small" type="submit">${icon('plus')}${esc(t('subtask.add'))}</button></form>
  </section>`;
}

// A step being typed survives a background redraw of the screen (text and focus).
const stepDraft = { taskId: null, text: '', focused: false };

// One delegated handler: the task screen is redrawn from cache without re-mounting,
// and a form without a listener would otherwise submit natively (a page reload).
if (globalThis.document?.addEventListener) {
  document.addEventListener('input', (event) => {
    const input = event.target.closest?.('[data-subtask-title]');
    if (!input) return;
    stepDraft.taskId = input.closest('[data-subtask-form]')?.dataset.taskId || null;
    stepDraft.text = input.value;
  });
  // A real checkbox: its own state is the user's answer (a click handler that
  // prevents the default would flip it back until the redraw).
  document.addEventListener('change', async (event) => {
    const box = event.target.closest?.('[data-subtask-toggle]');
    if (!box) return;
    const payload = { task_id: box.dataset.taskId };
    if (box.checked) payload.occurred_at = new Date().toISOString();
    await change(box.checked ? 'subtask.complete' : 'subtask.reopen', box.dataset.id, payload);
  });
  document.addEventListener('focusin', (event) => { if (event.target.closest?.('[data-subtask-title]')) stepDraft.focused = true; });
  document.addEventListener('focusout', (event) => { if (event.target.closest?.('[data-subtask-title]')) stepDraft.focused = false; });
  document.addEventListener('submit', async (event) => {
    const form = event.target.closest?.('[data-subtask-form]');
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector('[data-subtask-title]');
    const title = input.value.trim();
    if (!title) return;
    input.value = '';
    stepDraft.text = '';
    await change('subtask.create', newEntityId('subtask'), { task_id: form.dataset.taskId, title });
    document.querySelector('[data-subtask-title]')?.focus();
  });
}

export default {
  id: 'task',
  tab: 'tasks',
  detail: true,
  title: () => t('task.title'),
  async load({ fresh, params }) {
    let result = await load('/api/v1/tasks', { fresh });
    // A deep link (push tap, another device) may name a task newer than the cached list.
    if (!fresh && !result.data.some((x) => x.id === params[0])) result = await load('/api/v1/tasks', { fresh: true }).catch(() => result);
    const task = result.data.find((x) => x.id === params[0]) || null;
    if (!task) return { ...result, data: null };
    const [attachments, active, history, checklist] = await Promise.all([
      load(`/api/v1/attachments?owner_kind=OBLIGATION&owner_id=${encodeURIComponent(task.id)}`, { fresh }).catch(() => ({ data: [] })),
      load('/api/v1/execution/active', { fresh }).catch(() => ({ data: { session: null } })),
      load(`/api/v1/execution/sessions?task_id=${encodeURIComponent(task.id)}&days=365`, { fresh }).catch(() => ({ data: { sessions: [] } })),
      load(`/api/v1/tasks/${encodeURIComponent(task.id)}/subtasks`, { fresh }).catch(() => ({ data: { task_id: task.id, subtasks: [] } })),
    ]);
    const executionActive = active.data?.session?.task_id === task.id ? active.data.session : null;
    return { ...result, stale: result.stale || attachments.stale || active.stale || history.stale,
      data: { ...task, attachments: attachments.data, execution_active: executionActive, execution_sessions: history.data?.sessions || [],
        subtasks: checklist.data?.subtasks || [] } };
  },
  render(task) {
    if (!task) return empty(t('task.missing'), t('task.missingHint'), 'tasks');
    const total = Number(task.estimated_total_effort_minutes || 0);
    const left = Number(task.remaining_effort_minutes || 0);
    const pct = task.count_progress?.total ? Math.round((task.count_progress.done / task.count_progress.total) * 100)
      : total ? Math.round(((total - left) / total) * 100) : 0;
    const cutoff = task.actual_cutoff || {};
    const open = task.status === 'ACTIVE' || task.status === 'DRAFT';
    const place = taskPlace(task);
    const active = task.status === 'ACTIVE';
    const advice = active ? startAdvice(task) : null;
    const conflict = task.cutoff_truth?.state === 'CONFLICT';
    return `
      <section class="detail-head">
        <div class="chips">${active ? riskChip(task.risk) : chip(place === 'archive' ? t('place.archive') : code('status', task.status), task.status === 'COMPLETED' ? 'ok' : task.status === 'DRAFT' ? 'warn' : 'muted')}
          ${task.importance !== 'NORMAL' ? chip(code('importanceShort', task.importance), task.importance === 'LOW' ? 'muted' : 'warn') : ''}
          ${task.category !== 'GENERAL' ? chip(code('category', task.category)) : ''}
          ${task.execution_active && open ? chip(t('task.inProgress'), 'accent') : ''}</div>
        <h2 class="detail-title">${esc(task.title)}</h2>
        ${task.description ? `<p class="muted pre">${esc(task.description)}</p>` : ''}
        ${advice ? `<p class="advice">${icon('clock')} ${esc(advice)}</p>` : ''}
      </section>

      ${task.execution_active ? `<section class="section">${executionCard(task.execution_active, task)}</section>` : ''}

      ${task.status === 'DRAFT' ? `<section class="card question-card">
        <strong>${esc(t('q.effort'))}</strong>
        <div class="chip-row">${DURATION_PRESETS.map((m) => [m, fmtDuration(m)])
          .map(([m, label]) => `<button type="button" class="chip-toggle" data-action="detail-effort" data-minutes="${m}">${esc(label)}</button>`).join('')}
          <button type="button" class="chip-toggle" data-action="detail-edit" data-focus="effort">${esc(t('duration.other'))}</button></div>
        <small class="help">${esc(t('task.draftHelp'))}</small>
      </section>` : `<section class="card effort-card">
        <div class="effort-row"><span>${esc(t('task.effort'))}</span><strong>${esc(task.effort_estimate_source === 'SYSTEM_PROVISIONAL' ? t('task.provisional', { lo: fmtDuration(task.remaining_effort_low_minutes ?? 15), hi: fmtDuration(task.remaining_effort_high_minutes ?? 60) }) : t('task.effortValue', { left: fmtDuration(left), total: fmtDuration(total) }))}</strong></div>
        ${task.count_progress ? `<div class="effort-row"><span>${esc(t('task.countProgress'))}</span><strong>${esc(t('task.countValue', { done: task.count_progress.done, total: task.count_progress.total, unit: task.count_progress.unit || '', pct: Math.round((task.count_progress.done / task.count_progress.total) * 100) }))}</strong></div>` : ''}
        <span class="progress big" aria-label="${pct}%"><span data-w="${pct}"></span></span>
        ${task.remaining_effort_low_minutes != null && task.remaining_effort_high_minutes != null ? `<p class="help">${esc(t('task.range', { lo: fmtDuration(task.remaining_effort_low_minutes), hi: fmtDuration(task.remaining_effort_high_minutes) }))}</p>` : ''}
        ${active ? `<div class="button-row">
          ${task.execution_active ? '' : `<button class="button primary" data-action="detail-start">${esc(t('today.start'))}</button>`}
          <button class="button ${task.started_at ? 'primary' : ''}" data-action="detail-progress">${icon('check')}${esc(t('task.logProgress'))}</button></div>` : ''}
      </section>`}

      ${checklistCard(task, total)}

      <section class="card">
        <dl class="kv-list">
          ${kv(t('task.cutoff'), cutoff.state === 'KNOWN' ? when(cutoff.at) : deadlineText(cutoff))}
          ${task.target_at ? kv(t('task.target'), when(task.target_at)) : ''}
          ${task.actionable_from && new Date(task.actionable_from) > now() ? kv(t('task.actionableFrom'), when(task.actionable_from)) : ''}
          ${task.remind_at ? kv(t('task.remind'), when(task.remind_at)) : ''}
          ${task.splittable ? kv(t('task.chunks'), t('task.chunksSplit', { min: fmtDuration(task.min_chunk_minutes || 30), max: fmtDuration(task.max_chunk_minutes || 90) })) : ''}
          ${task.completed_at ? kv(t('task.completedAt'), fmtDateTime(task.completed_at)) : ''}
        </dl>
        ${conflict ? `<div class="banner warn">${icon('alert')}<div><p>${esc(t('task.conflictHelp'))}</p></div></div>` : ''}
      </section>

      <section class="detail-actions">
        ${open ? `
          <button class="button ok" data-action="detail-lifecycle" data-op="complete">${icon('check')}${esc(t('lifecycle.complete'))}</button>
          <button class="button" data-action="detail-reschedule">${icon('calendar')}${esc(t('task.reschedule'))}</button>
          <button class="button" data-action="detail-edit">${esc(t('task.edit'))}</button>
          <button class="button danger ghost" data-action="detail-lifecycle" data-op="cancel">${esc(t('lifecycle.cancel'))}</button>`
        : place === 'archive' ? `<button class="button primary" data-action="detail-lifecycle" data-op="restore">${icon('repeat')}${esc(t(task.status === 'ARCHIVED' && task.completed_at ? 'lifecycle.restoreDone' : 'lifecycle.reopen'))}</button>`
        : `<button class="button primary" data-action="detail-lifecycle" data-op="reopen">${icon('repeat')}${esc(t('lifecycle.reopen'))}</button>
           <button class="button" data-action="detail-lifecycle" data-op="archive">${esc(t('lifecycle.archive'))}</button>`}
        <button class="button danger ghost detail-delete" data-action="detail-lifecycle" data-op="delete">${esc(t('lifecycle.delete'))}</button>
      </section>
      <p class="help pad">${esc(t(`lifecycle.help.${open ? 'open' : place === 'archive' ? 'ARCHIVED' : task.status}`))}</p>

      <section class="card">
        <h3>${esc(t('execution.history'))}</h3>
        ${(() => {
          const sessions = task.execution_sessions || [];
          const total = sessions.filter((s) => s.state === 'FINISHED').reduce((sum, s) => sum + Number(s.actual_work_seconds || 0), 0);
          if (!sessions.length) return `<p class="muted">${esc(t('execution.noHistory'))}</p>`;
          return `<p class="muted">${esc(t('execution.totalWorked', { d: fmtDuration(Math.round(total / 60)) }))}</p>
            <div class="list">${sessions.slice(0, 10).map((s) => `<div class="row">
              <span class="row-main"><strong>${esc(fmtDateTime(s.started_at))}</strong><small>${esc(code('executionState', s.state))}</small></span>
              <span class="row-aside">${esc(fmtDuration(Math.max(1, Math.round(Number(s.actual_work_seconds || 0) / 60))))}</span>
            </div>`).join('')}</div>`;
        })()}
      </section>

      <section class="card">
        <h3>${esc(t('task.attachments'))}</h3>
        <div class="list">${task.attachments?.map((item) => `<a class="row" href="/api/v1/attachments/${encodeURIComponent(item.id)}/download" download>
          <span class="row-main"><strong>${esc(item.original_name)}</strong></span></a>`).join('') || `<p class="muted">${esc(t('task.noAttachments'))}</p>`}</div>
        <input class="hidden" type="file" data-attachment-input>
        <button class="button ghost wide" data-action="attachment-pick">${esc(t('task.addAttachment'))}</button>
      </section>`;
  },
  actions: {
    async 'subtask-menu'(el, ctx) {
      const items = ctx.data?.subtasks || [];
      const index = Number(el.dataset.index);
      const item = items[index];
      if (!item) return;
      const choice = await actionSheet({ title: item.title, items: [
        { id: 'rename', icon: 'note', label: t('subtask.rename') },
        { id: 'effort', icon: 'clock', label: t('subtask.effort') },
        ...(index > 0 ? [{ id: 'up', icon: 'back', label: t('subtask.up') }] : []),
        ...(index < items.length - 1 ? [{ id: 'down', icon: 'chevron', label: t('subtask.down') }] : []),
        { id: 'delete', icon: 'x', label: t('lifecycle.delete'), tone: 'danger' },
      ] });
      if (choice === 'up' || choice === 'down') {
        // Between the neighbours: a fractional position, so offline moves need no renumbering.
        const target = choice === 'up' ? index - 1 : index + 1;
        const beyond = choice === 'up' ? items[index - 2] : items[index + 2];
        const edge = items[target].position + (choice === 'up' ? -1 : 1);
        const position = beyond ? (items[target].position + beyond.position) / 2 : edge;
        await change('subtask.move', item.id, { task_id: item.task_id, position });
      } else if (choice === 'delete') {
        await change('subtask.delete', item.id, { task_id: item.task_id }, { success: t('subtask.deleted') });
      } else if (choice === 'rename' || choice === 'effort') {
        const dialog = openSheet({ title: t(choice === 'rename' ? 'subtask.rename' : 'subtask.effort'),
          body: choice === 'rename'
            ? `<label class="field"><span>${esc(t('form.title'))}</span><input data-subtask-edit maxlength="300" value="${esc(item.title)}"></label>`
            : `<label class="field"><span>${esc(t('subtask.minutes'))}</span><input type="number" min="1" max="100000" data-subtask-edit value="${esc(item.effort_minutes ?? '')}"></label><p class="help">${esc(t('subtask.effortHelp'))}</p>`,
          actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>` });
        dialog.querySelector('[data-save]').addEventListener('click', async () => {
          const value = dialog.querySelector('[data-subtask-edit]').value.trim();
          dialog.close('saved');
          if (choice === 'rename' && value && value !== item.title) await change('subtask.update', item.id, { task_id: item.task_id, title: value });
          if (choice === 'effort') await change('subtask.update', item.id, { task_id: item.task_id, effort_minutes: value ? Math.round(Number(value)) : null });
        });
      }
    },
    'detail-progress'(_el, ctx) { logProgress(ctx.data); },
    'detail-edit'(el, ctx) { editTaskSheet(ctx.data, { focus: el.dataset.focus }); },
    'detail-reschedule'(_el, ctx) { rescheduleSheet(ctx.data); },
    async 'detail-start'(_el, ctx) {
      await startExecution(ctx.data, null);
    },
    async 'execution-pause'(_el, ctx) {
      if (ctx.data.execution_active) await pauseExecution(ctx.data.execution_active);
    },
    async 'execution-resume'(_el, ctx) {
      if (ctx.data.execution_active) await resumeExecution(ctx.data.execution_active);
    },
    async 'execution-finish'(_el, ctx) {
      if (ctx.data.execution_active) await finishExecution(ctx.data.execution_active, ctx.data);
    },
    'execution-review'(_el, ctx) {
      const session = ctx.data?.execution_active;
      const task = ctx.data;
      if (session && task) reviewLongExecution(session, task);
    },
    async 'execution-complete'(_el, ctx) {
      if (ctx.data.execution_active) await finishExecution(ctx.data.execution_active, ctx.data, { complete: true });
    },
    async 'detail-effort'(el, ctx) {
      const minutes = Number(el.dataset.minutes);
      await change('task.update', ctx.data.id, { estimated_total_effort_minutes: minutes, remaining_effort_minutes: minutes }, { success: t('task.inPlan') });
    },
    'attachment-pick'(el) { el.closest('.card').querySelector('[data-attachment-input]').click(); },
    'detail-lifecycle'(el, ctx) { lifecycle(ctx.data.id, ctx.data.version, el.dataset.op, { title: ctx.data.title, from: ctx.data.status }); },
  },
  mount(root, task, ctx) {
    mountExecutionTimers(root, task?.execution_active || null, task);
    if (stepDraft.focused && stepDraft.taskId === task?.id) {
      const input = root.querySelector('[data-subtask-title]');
      input?.focus();
      input?.setSelectionRange?.(input.value.length, input.value.length);
    }
    root.querySelector('[data-attachment-input]')?.addEventListener('change', async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const content = await fileBase64(file);
      await mutate(() => api('/api/v1/attachments', { method: 'POST', body: {
        owner_kind: 'OBLIGATION', owner_id: task.id, original_name: file.name,
        mime_type: file.type || 'application/octet-stream', content_base64: content,
      } }), { success: t('task.attachmentAdded') });
    });
    // "Перенести" from a notification or the Today card opens straight into rescheduling.
    if (task && ctx.query?.step === 'reschedule' && !this._openedFor?.has(task.id)) {
      this._openedFor = new Set([...(this._openedFor || []), task.id]);
      history.replaceState(null, '', `#/task/${encodeURIComponent(task.id)}`);
      rescheduleSheet(task);
    }
  },
};
