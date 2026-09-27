import { load } from '../store.js';
import { t, fmtDuration } from '../i18n.js';
import { esc, icon, chip, riskChip, empty, openSheet, setBusy } from '../ui.js';
import { change, shell } from '../actions.js';
import { newEntityId } from '../sync.js';

function projectCard(project) {
  const progress = project.progress || {};
  return `<button class="task-card agenda-row" data-action="open-project" data-id="${esc(project.id)}">
    <span class="task-top">
      <span class="kind-icon kind-task">${icon('plan')}</span>
      <strong class="task-title">${esc(project.title)}</strong>
      ${project.status === 'ACTIVE' ? riskChip(project.risk) : chip(t('project.status.' + project.status), 'muted')}
    </span>
    <span class="task-meta">
      <span>${esc(t('project.tasksProgress', { done: progress.tasks_completed || 0, total: progress.tasks_total || 0 }))}</span>
      ${progress.remaining_effort_minutes != null ? `<span>${esc(t('tasks.left', { d: fmtDuration(progress.remaining_effort_minutes) }))}</span>` : ''}
    </span>
    <span class="progress-row"><span class="progress" aria-hidden="true"><span data-w="${Number(progress.percent || 0)}"></span></span>
      <small>${esc(t('project.percent', { n: Number(progress.percent || 0) }))}</small>
      ${project._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}
    </span>
  </button>`;
}

function createProject() {
  const dialog = openSheet({
    title: t('project.new'),
    body: `<label class="field"><span>${esc(t('project.name'))}</span>
        <input type="text" data-project-title maxlength="200" autocomplete="off">
      </label>
      <label class="field"><span>${esc(t('form.description'))}</span>
        <textarea data-project-description rows="3" maxlength="5000"></textarea>
      </label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-create-project>${esc(t('compose.create'))}</button>`,
  });
  const title = dialog.querySelector('[data-project-title]');
  title?.focus();
  dialog.querySelector('[data-create-project]').addEventListener('click', async (event) => {
    const value = String(title.value || '').trim();
    if (!value) { title.focus(); return; }
    setBusy(event.currentTarget, true);
    const id = newEntityId('project');
    const result = await change('project.create', id, {
      title: value,
      description: String(dialog.querySelector('[data-project-description]').value || '').trim() || null,
    }, { success: t('project.created') });
    if (result) {
      dialog.close('created');
      shell.go('project', { params: [id] });
    } else setBusy(event.currentTarget, false);
  });
}

export default {
  id: 'projects',
  tab: 'more',
  title: () => t('nav.projects'),
  async load({ fresh }) {
    return load('/api/v1/projects', { fresh });
  },
  render(projects) {
    const active = (projects || []).filter((p) => p.status === 'ACTIVE');
    const closed = (projects || []).filter((p) => p.status !== 'ACTIVE');
    return `<section class="section">
      <div class="section-head"><div><h2>${esc(t('project.active'))}</h2><p>${esc(t('project.help'))}</p></div>
        <button class="button primary" data-action="create-project">${icon('plus')}${esc(t('project.new'))}</button></div>
      ${active.length ? `<div class="task-list">${active.map(projectCard).join('')}</div>`
        : empty(t('project.empty'), t('project.emptyHelp'), 'plan')}
    </section>
    ${closed.length ? `<section class="section"><h2>${esc(t('project.closed'))}</h2>
      <div class="task-list">${closed.map(projectCard).join('')}</div></section>` : ''}`;
  },
  actions: {
    'create-project'() { createProject(); },
    'open-project'(el) { shell.go('project', { params: [el.dataset.id] }); },
  },
};
