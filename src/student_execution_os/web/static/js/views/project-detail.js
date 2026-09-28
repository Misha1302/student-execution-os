import { load } from '../store.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative } from '../i18n.js';
import { esc, icon, chip, riskChip, empty, openSheet, setBusy, localInputValue, isoFromLocalInput } from '../ui.js';
import { durationPicker, takeDuration } from '../duration.js';
import { change, shell } from '../actions.js';
import { newEntityId } from '../sync.js';

function taskRow(task) {
  const done = task.status === 'COMPLETED';
  return `<div class="row" data-id="${esc(task.id)}">
    <button class="icon-button" data-action="${done ? 'project-reopen-task' : 'project-complete-task'}" data-id="${esc(task.id)}"
      aria-label="${esc(done ? t('lifecycle.reopen') : t('lifecycle.complete'))}">${icon(done ? 'check' : 'task')}</button>
    <button class="row-main" data-action="open-task" data-id="${esc(task.id)}">
      <strong>${esc(task.title)}</strong>
      <small>${task.remaining_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(t('tasks.left', { d: fmtDuration(task.remaining_effort_minutes) }))}</small>
    </button>
    ${task.risk ? riskChip(task.risk) : ''}
    <button class="icon-button" data-action="project-remove-task" data-id="${esc(task.id)}" aria-label="${esc(t('project.removeTask'))}">×</button>
  </div>`;
}

function milestoneRow(m) {
  const done = m.status === 'COMPLETED';
  return `<div class="row" data-id="${esc(m.id)}">
    <button class="icon-button" data-action="${done ? 'milestone-reopen' : 'milestone-complete'}" data-id="${esc(m.id)}"
      aria-label="${esc(done ? t('lifecycle.reopen') : t('lifecycle.complete'))}">${icon(done ? 'check' : 'flag')}</button>
    <span class="row-main"><strong>${esc(m.title)}</strong>
      <small>${esc(fmtDateTime(m.marker_at))} · ${esc(fmtRelative(m.marker_at))}${m.consequence ? ` · ${esc(m.consequence)}` : ''}</small></span>
    ${m.overdue && m.status === 'ACTIVE' ? chip(t('project.overdueMilestone'), 'danger') : chip(code('milestoneRole', m.role), 'muted')}
    <button class="icon-button" data-action="milestone-delete" data-id="${esc(m.id)}" aria-label="${esc(t('project.removeMilestone'))}">×</button>
  </div>`;
}

function addTask(project) {
  const dialog = openSheet({
    eyebrow: project.title,
    title: t('project.addTask'),
    body: `<label class="field"><span>${esc(t('form.title'))}</span><input data-project-task-title maxlength="300"></label>
      <div class="field"><span>${esc(t('form.effort'))}</span>${durationPicker('project-task-effort', 60)}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-project-task-save>${esc(t('compose.create'))}</button>`,
  });
  dialog.querySelector('[data-project-task-title]')?.focus();
  dialog.querySelector('[data-project-task-save]').addEventListener('click', async (event) => {
    const title = String(dialog.querySelector('[data-project-task-title]').value || '').trim();
    const effort = takeDuration(dialog, 'project-task-effort');
    if (!title || !effort) return;
    setBusy(event.currentTarget, true);
    const result = await change('project.task.create', project.id, {
      task_id: newEntityId('task'),
      title, estimated_total_effort_minutes: effort,
      actual_cutoff: { state: 'ABSENT' }, splittable: effort > 30,
      min_chunk_minutes: effort > 30 ? 15 : null,
      max_chunk_minutes: effort > 30 ? Math.min(90, effort) : null,
    }, { success: t('project.taskAdded') });
    if (result) dialog.close('created'); else setBusy(event.currentTarget, false);
  });
}

function addMilestone(project) {
  const defaultAt = new Date(Date.now() + 7 * 86400000);
  defaultAt.setSeconds(0, 0);
  const dialog = openSheet({
    eyebrow: project.title,
    title: t('project.addMilestone'),
    body: `<label class="field"><span>${esc(t('form.title'))}</span><input data-milestone-title maxlength="300"></label>
      <label class="field"><span>${esc(t('project.when'))}</span><input type="datetime-local" data-milestone-at value="${esc(localInputValue(defaultAt))}"></label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-milestone-save>${esc(t('compose.create'))}</button>`,
  });
  dialog.querySelector('[data-milestone-save]').addEventListener('click', async (event) => {
    const title = String(dialog.querySelector('[data-milestone-title]').value || '').trim();
    const marker = isoFromLocalInput(dialog.querySelector('[data-milestone-at]').value);
    if (!title || !marker) return;
    setBusy(event.currentTarget, true);
    const result = await change('milestone.create', newEntityId('milestone'), {
      project_id: project.id, title, marker_at: marker, role: 'INTERMEDIATE',
    }, { success: t('project.milestoneAdded') });
    if (result) dialog.close('created'); else setBusy(event.currentTarget, false);
  });
}

export default {
  id: 'project',
  tab: 'more',
  detail: true,
  title: () => t('project.title'),
  async load({ fresh, params }) {
    return load(`/api/v1/projects/${encodeURIComponent(params[0])}`, { fresh });
  },
  render(project) {
    const progress = project.progress || {};
    const tasks = (project.members || []).filter((x) => x.kind === 'TASK');
    const events = (project.members || []).filter((x) => x.kind === 'EVENT');
    return `<section class="hero compact">
      <span class="eyebrow">${esc(t('project.title'))}</span>
      <h2>${esc(project.title)}</h2>
      ${project.description ? `<p>${esc(project.description)}</p>` : ''}
      <div class="chip-row">${project.status === 'ACTIVE' ? riskChip(project.risk) : chip(t('project.status.' + project.status), 'muted')}
        ${chip(t('project.percent', { n: progress.percent || 0 }), 'accent')}</div>
      <div class="progress-row"><span class="progress"><span data-w="${Number(progress.percent || 0)}"></span></span>
        <small>${esc(t('project.tasksProgress', { done: progress.tasks_completed || 0, total: progress.tasks_total || 0 }))}</small></div>
    </section>

    <section class="card">
      <div class="section-head"><h3>${esc(t('project.checklist'))}</h3>
        ${project.status === 'ACTIVE' ? `<button class="button ghost" data-action="project-add-task">${icon('plus')}${esc(t('project.addTask'))}</button>` : ''}</div>
      ${tasks.length ? `<div class="list">${tasks.map(taskRow).join('')}</div>` : `<p class="muted">${esc(t('project.noTasks'))}</p>`}
    </section>

    <section class="card">
      <div class="section-head"><h3>${esc(t('project.milestones'))}</h3>
        ${project.status === 'ACTIVE' ? `<button class="button ghost" data-action="project-add-milestone">${icon('plus')}${esc(t('project.addMilestone'))}</button>` : ''}</div>
      ${project.milestones?.length ? `<div class="list">${project.milestones.map(milestoneRow).join('')}</div>` : `<p class="muted">${esc(t('project.noMilestones'))}</p>`}
    </section>

    ${events.length ? `<section class="card"><h3>${esc(t('project.linkedEvents'))}</h3>
      <div class="list">${events.map((e) => `<button class="row" data-action="open-event" data-id="${esc(e.id)}"><span class="row-main"><strong>${esc(e.title)}</strong><small>${esc(fmtDateTime(e.starts_at))}</small></span></button>`).join('')}</div></section>` : ''}

    <section class="section"><div class="now-actions">
      ${project.status === 'ACTIVE' ? `<button class="button primary" data-action="project-complete">${esc(t('project.complete'))}</button>
        <button class="button ghost" data-action="project-cancel">${esc(t('project.cancel'))}</button>`
        : `<button class="button primary" data-action="project-reopen">${esc(t('project.reopen'))}</button>`}
    </div></section>`;
  },
  actions: {
    'project-add-task'(_el, ctx) { addTask(ctx.data); },
    'project-add-milestone'(_el, ctx) { addMilestone(ctx.data); },
    async 'project-complete'(_el, ctx) { await change('project.complete', ctx.data.id, { expected_version: ctx.data.version }, { success: t('project.completed') }); },
    async 'project-cancel'(_el, ctx) { await change('project.cancel', ctx.data.id, { expected_version: ctx.data.version }, { success: t('project.cancelled') }); },
    async 'project-reopen'(_el, ctx) { await change('project.reopen', ctx.data.id, { expected_version: ctx.data.version }, { success: t('project.reopened') }); },
    async 'project-complete-task'(el) { await change('task.complete', el.dataset.id, { occurred_at: new Date().toISOString() }, { success: t('lifecycle.done.complete') }); },
    async 'project-reopen-task'(el) { await change('task.reopen', el.dataset.id, {}, { success: t('lifecycle.done.reopen') }); },
    async 'project-remove-task'(el, ctx) { await change('project.member.remove', ctx.data.id, { obligation_id: el.dataset.id }, { success: t('project.taskRemoved') }); },
    async 'milestone-complete'(el, ctx) {
      const m = ctx.data.milestones.find((x) => x.id === el.dataset.id);
      if (m) await change('milestone.complete', m.id, { expected_version: m.version }, { success: t('project.milestoneCompleted') });
    },
    async 'milestone-reopen'(el, ctx) {
      const m = ctx.data.milestones.find((x) => x.id === el.dataset.id);
      if (m) await change('milestone.reopen', m.id, { expected_version: m.version }, { success: t('project.milestoneReopened') });
    },
    async 'milestone-delete'(el, ctx) {
      const m = ctx.data.milestones.find((x) => x.id === el.dataset.id);
      if (m) await change('milestone.delete', m.id, { expected_version: m.version }, { success: t('project.milestoneRemoved') });
    },
    'open-task'(el) { shell.go('task', { params: [el.dataset.id] }); },
    'open-event'(el, ctx) { import('../events.js').then(({ eventSheet }) => {
      const event = (ctx.data.members || []).find((x) => x.kind === 'EVENT' && x.id === el.dataset.id);
      if (event) eventSheet(event);
    }); },
  },
};
