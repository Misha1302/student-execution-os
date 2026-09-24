import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative } from '../i18n.js';
import { esc, icon, chip, riskChip, kv, empty, openSheet, localInputValue, isoFromLocalInput, setBusy } from '../ui.js';
import { lifecycle, logProgress, mutate } from '../actions.js';
import { queueOperation } from '../sync.js';

const sameInstant = (a, b) => (!a && !b) || (a && b && new Date(a).getTime() === new Date(b).getTime());

function editSheet(task) {
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('task.edit'),
    body: `<div class="form">
      <label class="field"><span>${esc(t('form.remaining'))}</span>
        <input type="number" inputmode="numeric" min="0" step="5" data-f="remaining" value="${esc(task.remaining_effort_minutes)}"></label>
      <label class="field"><span>${esc(t('form.target'))}</span>
        <input type="datetime-local" data-f="target" value="${esc(localInputValue(task.target_at))}"></label>
      <label class="field"><span>${esc(t('form.actionableFrom'))}</span>
        <input type="datetime-local" data-f="actionable" value="${esc(localInputValue(task.actionable_from))}"></label>
      <p class="help">${esc(t('task.editHelp'))}</p>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const f = (name) => dialog.querySelector(`[data-f="${name}"]`).value;
    const candidate = { remaining_effort_minutes: Number(f('remaining') || 0),
      target_at: isoFromLocalInput(f('target')), actionable_from: isoFromLocalInput(f('actionable')) };
    const changes = {};
    if (candidate.remaining_effort_minutes !== Number(task.remaining_effort_minutes || 0)) changes.remaining_effort_minutes = candidate.remaining_effort_minutes;
    if (!sameInstant(candidate.target_at, task.target_at)) changes.target_at = candidate.target_at;
    if (!sameInstant(candidate.actionable_from, task.actionable_from)) changes.actionable_from = candidate.actionable_from;
    if (!Object.keys(changes).length) { dialog.close('unchanged'); return; }
    setBusy(e.currentTarget, true);
    await mutate(async () => {
      const result = await queueOperation('task.update', task.id, changes, { optimisticTask: { ...task, ...changes } });
      return result.entity || result;
    }, { success: t('task.saved') });
    dialog.close('saved');
  });
}

function refineSheet(task) {
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('task.refine'),
    body: `<label class="field"><span>${esc(t('form.effort'))}</span><input type="number" inputmode="numeric" min="1" step="5" data-effort autofocus></label>
      <p class="help">${esc(t('task.refineHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async (event) => {
    const effort = Number(dialog.querySelector('[data-effort]').value);
    if (!effort) return;
    setBusy(event.currentTarget, true);
    await mutate(async () => {
      const changes = { estimated_total_effort_minutes: effort, remaining_effort_minutes: effort };
      const result = await queueOperation('task.update', task.id, changes, { optimisticTask: { ...task, ...changes, status: 'ACTIVE' } });
      return result.entity || result;
    }, { success: t('task.saved') });
    dialog.close('saved');
  });
}

function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] || '');
    reader.readAsDataURL(file);
  });
}

export default {
  id: 'task',
  tab: 'tasks',
  detail: true,
  title: () => t('task.title'),
  async load({ fresh, params }) {
    const result = await load('/api/v1/tasks', { fresh });
    const task = result.data.find((x) => x.id === params[0]) || null;
    if (!task) return { ...result, data: null };
    const attachments = await load(`/api/v1/attachments?owner_kind=OBLIGATION&owner_id=${encodeURIComponent(task.id)}`, { fresh });
    return { ...result, stale: result.stale || attachments.stale, data: { ...task, attachments: attachments.data } };
  },
  render(task) {
    if (!task) return empty(t('task.missing'), t('task.missingHint'), 'tasks');
    const truth = task.cutoff_truth;
    const total = Number(task.estimated_total_effort_minutes || 0);
    const left = Number(task.remaining_effort_minutes || 0);
    const pct = total ? Math.round(((total - left) / total) * 100) : 0;
    const cutoff = task.actual_cutoff || {};
    const active = task.status === 'ACTIVE';
    return `
      <section class="detail-head">
        <div class="chips">${active ? riskChip(task.risk) : chip(code('status', task.status), task.status === 'COMPLETED' ? 'ok' : 'muted')}
          ${chip(code('importance', task.importance), task.importance === 'CRITICAL' || task.importance === 'HIGH' ? 'warn' : '')}
          ${chip(code('category', task.category))}
          ${chip(code('own', 'CANONICAL'), 'canonical')}</div>
        <h2 class="detail-title">${esc(task.title)}</h2>
        ${task.description ? `<p class="muted">${esc(task.description)}</p>` : ''}
      </section>

      <section class="card effort-card">
        ${task.status === 'DRAFT' ? `<div class="banner warn"><div><strong>${esc(t('today.needsRefinement'))}</strong><p>${esc(t('task.refineHelp'))}</p></div></div>` : ''}
        <div class="effort-row"><span>${esc(t('task.effort'))}</span><strong>${esc(t('task.effortValue', { left: fmtDuration(left), total: fmtDuration(total) }))}</strong></div>
        <span class="progress big" aria-label="${pct}%"><span data-w="${pct}"></span></span>
        ${task.remaining_effort_low_minutes != null && task.remaining_effort_high_minutes != null ? `<p class="help">${esc(t('task.range', { lo: fmtDuration(task.remaining_effort_low_minutes), hi: fmtDuration(task.remaining_effort_high_minutes) }))}</p>` : ''}
        ${active ? `<button class="button primary wide" data-action="detail-progress">${icon('check')}${esc(t('task.logProgress'))}</button>` : ''}
      </section>

      <section class="card">
        <dl class="kv-list">
          ${kv(t('task.cutoff'), cutoff.state === 'KNOWN' ? `${fmtDateTime(cutoff.at)} · ${fmtRelative(cutoff.at)}` : code('cutoffState', cutoff.state))}
          ${kv(t('task.target'), task.target_at ? `${fmtDateTime(task.target_at)} · ${fmtRelative(task.target_at)}` : '—')}
          ${kv(t('task.lss'), task.risk?.latest_safe_start ? fmtDateTime(task.risk.latest_safe_start) : '—')}
          ${kv(t('task.actionableFrom'), task.actionable_from ? fmtDateTime(task.actionable_from) : '—')}
          ${kv(t('task.chunks'), task.splittable ? t('task.chunksSplit', { min: fmtDuration(task.min_chunk_minutes), max: fmtDuration(task.max_chunk_minutes) }) : t('task.chunksWhole'))}
        </dl>
        ${task.risk?.reasons?.length ? `<p class="help">${esc(task.risk.reasons.map((r) => code('reason', r)).join(' · '))}</p>` : ''}
      </section>

      ${truth ? `<section class="card ${truth.state === 'CONFLICT' ? 'card-danger' : ''}">
        <h3>${icon('evidence')} ${esc(t('task.provenance'))}</h3>
        <p>${chip(code('effective', truth.state), truth.state === 'CONFLICT' ? 'danger' : truth.state === 'OVERRIDDEN' ? 'warn' : 'ok')}</p>
        <dl class="kv-list">
          ${truth.reason ? kv(t('task.reason'), truth.reason) : ''}
          ${kv(t('task.evidence'), (truth.evidence_ids || []).join(', ') || '—')}
          ${truth.planning_projection?.at ? kv(t('task.projection'), fmtDateTime(truth.planning_projection.at)) : ''}
          ${kv(t('task.policy'), truth.policy_version)}
        </dl>
        ${truth.state === 'CONFLICT' ? `<p class="help">${esc(t('task.conflictHelp'))}</p>` : ''}
      </section>` : ''}

      <section class="card">
        <h3>${esc(t('task.attachments'))}</h3>
        <div class="list">${task.attachments?.map((item) => `<a class="row" href="/api/v1/attachments/${encodeURIComponent(item.id)}/download" download>
          <span class="row-main"><strong>${esc(item.original_name)}</strong><small>${esc(item.mime_type)} · ${esc(fmtDuration(Math.max(1, Math.round(item.size_bytes / 60000))))}</small></span></a>`).join('') || `<p class="muted">${esc(t('task.noAttachments'))}</p>`}</div>
        <input class="hidden" type="file" data-attachment-input>
        <button class="button ghost wide" data-action="attachment-pick">${esc(t('task.addAttachment'))}</button>
      </section>

      <section class="detail-actions">
        ${active ? `
          <button class="button" data-action="detail-edit">${esc(t('task.edit'))}</button>
          <button class="button ok" data-action="detail-lifecycle" data-op="complete">${icon('check')}${esc(t('lifecycle.complete'))}</button>
          <button class="button danger ghost" data-action="detail-lifecycle" data-op="cancel">${esc(t('lifecycle.cancel'))}</button>`
        : task.status === 'DRAFT' ? `<button class="button" data-action="detail-refine">${esc(t('task.refine'))}</button>
          ${task.estimated_total_effort_minutes ? `<button class="button primary" data-action="detail-activate">${esc(t('task.activate'))}</button>` : ''}
          <button class="button danger ghost" data-action="detail-lifecycle" data-op="cancel">${esc(t('lifecycle.cancel'))}</button>`
        : `<button class="button" data-action="detail-lifecycle" data-op="reopen">${icon('repeat')}${esc(t('lifecycle.reopen'))}</button>`}
      </section>
      <p class="help center">${esc(t('task.version', { v: task.version }))}</p>`;
  },
  actions: {
    'detail-progress'(_el, ctx) { logProgress(ctx.data); },
    'detail-edit'(_el, ctx) { editSheet(ctx.data); },
    'detail-refine'(_el, ctx) { refineSheet(ctx.data); },
    async 'detail-activate'(_el, ctx) {
      await mutate(async () => {
        const result = await queueOperation('task.activate', ctx.data.id, {}, { optimisticTask: { ...ctx.data, status: 'ACTIVE' } });
        return result.entity || result;
      }, { success: t('task.activated') });
    },
    'attachment-pick'(el) { el.closest('.card').querySelector('[data-attachment-input]').click(); },
    'detail-lifecycle'(el, ctx) { lifecycle(ctx.data.id, ctx.data.version, el.dataset.op, { title: ctx.data.title }); },
  },
  mount(root, task) {
    root.querySelector('[data-attachment-input]')?.addEventListener('change', async (event) => {
      const file = event.target.files?.[0];
      if (!file) return;
      const content = await fileBase64(file);
      await mutate(() => api('/api/v1/attachments', { method: 'POST', body: {
        owner_kind: 'OBLIGATION', owner_id: task.id, original_name: file.name,
        mime_type: file.type || 'application/octet-stream', content_base64: content,
      } }), { success: t('task.attachmentAdded') });
    });
  },
};
