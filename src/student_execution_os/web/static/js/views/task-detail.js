import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative } from '../i18n.js';
import { esc, icon, chip, riskChip, kv, empty, openSheet, localInputValue, isoFromLocalInput, setBusy } from '../ui.js';
import { lifecycle, logProgress, mutate } from '../actions.js';

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
    const payload = { expected_version: task.version, remaining_effort_minutes: Number(f('remaining') || 0) };
    payload.target_at = isoFromLocalInput(f('target'));
    payload.actionable_from = isoFromLocalInput(f('actionable'));
    setBusy(e.currentTarget, true);
    await mutate(() => api(`/api/v1/tasks/${encodeURIComponent(task.id)}`, { method: 'PATCH', body: payload }), { success: t('task.saved') });
    dialog.close('saved');
  });
}

export default {
  id: 'task',
  tab: 'tasks',
  detail: true,
  title: () => t('task.title'),
  async load({ fresh, params }) {
    const result = await load('/api/v1/tasks', { fresh });
    return { ...result, data: result.data.find((x) => x.id === params[0]) || null };
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

      <section class="detail-actions">
        ${active ? `
          <button class="button" data-action="detail-edit">${esc(t('task.edit'))}</button>
          <button class="button ok" data-action="detail-lifecycle" data-op="complete">${icon('check')}${esc(t('lifecycle.complete'))}</button>
          <button class="button danger ghost" data-action="detail-lifecycle" data-op="cancel">${esc(t('lifecycle.cancel'))}</button>`
        : `<button class="button" data-action="detail-lifecycle" data-op="reopen">${icon('repeat')}${esc(t('lifecycle.reopen'))}</button>`}
      </section>
      <p class="help center">${esc(t('task.version', { v: task.version }))}</p>`;
  },
  actions: {
    'detail-progress'(_el, ctx) { logProgress(ctx.data); },
    'detail-edit'(_el, ctx) { editSheet(ctx.data); },
    'detail-lifecycle'(el, ctx) { lifecycle(ctx.data.id, ctx.data.version, el.dataset.op, { title: ctx.data.title }); },
  },
};
