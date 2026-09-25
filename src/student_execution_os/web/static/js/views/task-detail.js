import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, riskChip, kv, empty, setBusy } from '../ui.js';
import { lifecycle, logProgress, mutate, change } from '../actions.js';
import { editTaskSheet, rescheduleSheet, deadlineText } from '../capture.js';

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
    const attachments = await load(`/api/v1/attachments?owner_kind=OBLIGATION&owner_id=${encodeURIComponent(task.id)}`, { fresh }).catch(() => ({ data: [] }));
    return { ...result, stale: result.stale || attachments.stale, data: { ...task, attachments: attachments.data } };
  },
  render(task) {
    if (!task) return empty(t('task.missing'), t('task.missingHint'), 'tasks');
    const total = Number(task.estimated_total_effort_minutes || 0);
    const left = Number(task.remaining_effort_minutes || 0);
    const pct = task.count_progress?.total ? Math.round((task.count_progress.done / task.count_progress.total) * 100)
      : total ? Math.round(((total - left) / total) * 100) : 0;
    const cutoff = task.actual_cutoff || {};
    const open = task.status === 'ACTIVE' || task.status === 'DRAFT';
    const active = task.status === 'ACTIVE';
    const advice = active ? startAdvice(task) : null;
    const conflict = task.cutoff_truth?.state === 'CONFLICT';
    return `
      <section class="detail-head">
        <div class="chips">${active ? riskChip(task.risk) : chip(code('status', task.status), task.status === 'COMPLETED' ? 'ok' : task.status === 'DRAFT' ? 'warn' : 'muted')}
          ${task.importance !== 'NORMAL' ? chip(code('importanceShort', task.importance), task.importance === 'LOW' ? 'muted' : 'warn') : ''}
          ${task.category !== 'GENERAL' ? chip(code('category', task.category)) : ''}
          ${task.started_at && open ? chip(t('task.inProgress'), 'accent') : ''}</div>
        <h2 class="detail-title">${esc(task.title)}</h2>
        ${task.description ? `<p class="muted pre">${esc(task.description)}</p>` : ''}
        ${advice ? `<p class="advice">${icon('clock')} ${esc(advice)}</p>` : ''}
      </section>

      ${task.status === 'DRAFT' ? `<section class="card question-card">
        <strong>${esc(t('q.effort'))}</strong>
        <div class="chip-row">${[[30, fmtDuration(30)], [60, fmtDuration(60)], [120, fmtDuration(120)], [180, fmtDuration(180)]]
          .map(([m, label]) => `<button type="button" class="chip-toggle" data-action="detail-effort" data-minutes="${m}">${esc(label)}</button>`).join('')}
          <button type="button" class="chip-toggle" data-action="detail-edit" data-focus="effort">${esc(t('form.custom'))}</button></div>
        <small class="help">${esc(t('task.draftHelp'))}</small>
      </section>` : `<section class="card effort-card">
        <div class="effort-row"><span>${esc(t('task.effort'))}</span><strong>${esc(t('task.effortValue', { left: fmtDuration(left), total: fmtDuration(total) }))}</strong></div>
        ${task.count_progress ? `<div class="effort-row"><span>${esc(t('task.countProgress'))}</span><strong>${esc(t('task.countValue', { done: task.count_progress.done, total: task.count_progress.total, unit: task.count_progress.unit || '', pct: Math.round((task.count_progress.done / task.count_progress.total) * 100) }))}</strong></div>` : ''}
        <span class="progress big" aria-label="${pct}%"><span data-w="${pct}"></span></span>
        ${task.remaining_effort_low_minutes != null && task.remaining_effort_high_minutes != null ? `<p class="help">${esc(t('task.range', { lo: fmtDuration(task.remaining_effort_low_minutes), hi: fmtDuration(task.remaining_effort_high_minutes) }))}</p>` : ''}
        ${active ? `<div class="button-row">
          ${task.started_at ? '' : `<button class="button primary" data-action="detail-start">${esc(t('today.start'))}</button>`}
          <button class="button ${task.started_at ? 'primary' : ''}" data-action="detail-progress">${icon('check')}${esc(t('task.logProgress'))}</button></div>` : ''}
      </section>`}

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
        : task.status === 'ARCHIVED' ? `<button class="button primary" data-action="detail-lifecycle" data-op="unarchive">${icon('repeat')}${esc(t('lifecycle.unarchive'))}</button>`
        : `<button class="button primary" data-action="detail-lifecycle" data-op="reopen">${icon('repeat')}${esc(t('lifecycle.reopen'))}</button>
           <button class="button" data-action="detail-lifecycle" data-op="archive">${esc(t('lifecycle.archive'))}</button>`}
        <button class="button danger ghost" data-action="detail-lifecycle" data-op="delete">${esc(t('lifecycle.delete'))}</button>
      </section>
      <p class="help pad">${esc(t(`lifecycle.help.${open ? 'open' : task.status}`))}</p>

      <section class="card">
        <h3>${esc(t('task.attachments'))}</h3>
        <div class="list">${task.attachments?.map((item) => `<a class="row" href="/api/v1/attachments/${encodeURIComponent(item.id)}/download" download>
          <span class="row-main"><strong>${esc(item.original_name)}</strong></span></a>`).join('') || `<p class="muted">${esc(t('task.noAttachments'))}</p>`}</div>
        <input class="hidden" type="file" data-attachment-input>
        <button class="button ghost wide" data-action="attachment-pick">${esc(t('task.addAttachment'))}</button>
      </section>`;
  },
  actions: {
    'detail-progress'(_el, ctx) { logProgress(ctx.data); },
    'detail-edit'(el, ctx) { editTaskSheet(ctx.data, { focus: el.dataset.focus }); },
    'detail-reschedule'(_el, ctx) { rescheduleSheet(ctx.data); },
    async 'detail-start'(el, ctx) {
      const task = ctx.data;
      await change('task.start', task.id, {}, { success: t('today.started') });
    },
    async 'detail-effort'(el, ctx) {
      const minutes = Number(el.dataset.minutes);
      await change('task.update', ctx.data.id, { estimated_total_effort_minutes: minutes, remaining_effort_minutes: minutes }, { success: t('task.inPlan') });
    },
    'attachment-pick'(el) { el.closest('.card').querySelector('[data-attachment-input]').click(); },
    'detail-lifecycle'(el, ctx) { lifecycle(ctx.data.id, ctx.data.version, el.dataset.op, { title: ctx.data.title }); },
  },
  mount(root, task, ctx) {
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

