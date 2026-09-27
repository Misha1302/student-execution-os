import { api } from './api.js';
import { change } from './actions.js';
import { newEntityId } from './sync.js';
import { t, fmtDateTime, fmtDuration } from './i18n.js';
import { esc, openSheet, toast, errorMessage, localInputValue, isoFromLocalInput } from './ui.js';

function previewCopy(preview) {
  if (!preview) return `<div class="banner warn"><div><strong>${esc(t('plan.controlOffline'))}</strong>
    <p>${esc(t('plan.controlOfflineHelp'))}</p></div></div>`;
  const before = preview.before?.feasibility_status || 'UNKNOWN';
  const after = preview.after?.feasibility_status || 'UNKNOWN';
  const changed = Number(preview.delta?.added_blocks || 0) + Number(preview.delta?.removed_blocks || 0);
  const worse = before !== 'INFEASIBLE' && after === 'INFEASIBLE';
  return `<div class="banner ${worse ? 'warn' : ''}"><div>
      <strong>${esc(t('plan.previewStatus', { before: t('status.title.' + before), after: t('status.title.' + after) }))}</strong>
      <p>${esc(t('plan.previewChanged', { n: changed }))}</p>
    </div></div>`;
}

export async function previewPlanControl(operation, { title, summary } = {}) {
  let preview = null;
  let offline = false;
  try {
    preview = await api('/api/v1/plan/control/preview', { method: 'POST', body: { operation } });
  } catch (err) {
    if (err?.code === 'NETWORK' || err?.code === 'NO_SERVER') offline = true;
    else {
      toast(errorMessage(err), { error: true });
      return null;
    }
  }
  return new Promise((resolve) => {
    const dialog = openSheet({
      eyebrow: t('plan.controlEyebrow'),
      title: title || t('plan.controlTitle'),
      body: `${summary ? `<p>${esc(summary)}</p>` : ''}
        ${previewCopy(preview)}
        <p class="help">${esc(offline ? t('plan.controlOfflineApply') : t('plan.controlPreviewHelp'))}</p>`,
      actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
        <button type="button" class="button primary" data-apply-control>${esc(t('plan.applyControl'))}</button>`,
      onClose: (value) => { if (value !== 'applied') resolve(null); },
    });
    dialog.querySelector('[data-apply-control]').addEventListener('click', async () => {
      const queued = await change(operation.type, operation.entity_id, operation.payload, { success: t('plan.controlQueued') });
      if (!queued) { resolve(null); return; }
      dialog.close('applied');
      resolve(queued);
    });
  });
}

export function pinOperation(item, startsAt = item.starts_at, endsAt = item.ends_at) {
  return {
    type: 'constraint.create',
    entity_id: newEntityId('constraint'),
    payload: {
      type: 'PINNED_WORK',
      task_id: item.ref?.obligation_id || item.ref?.task_id,
      starts_at: startsAt,
      ends_at: endsAt,
      reason: 'USER_PINNED_PLAN_WORK',
    },
  };
}

export function avoidOperation(item) {
  return {
    type: 'constraint.create',
    entity_id: newEntityId('constraint'),
    payload: {
      type: 'UNAVAILABLE',
      starts_at: item.starts_at,
      ends_at: item.ends_at,
      reason: 'USER_AVOIDED_PLAN_TIME',
    },
  };
}

export function moveConstraintOperation(constraint, startsAt, endsAt) {
  return {
    type: 'constraint.update',
    entity_id: constraint.id,
    payload: {
      expected_version: constraint.version,
      starts_at: startsAt,
      ends_at: endsAt,
    },
  };
}

export function deleteConstraintOperation(constraint) {
  return {
    type: 'constraint.delete',
    entity_id: constraint.id,
    payload: { expected_version: constraint.version },
  };
}

export function moveWorkSheet(item, { apply } = {}) {
  const start = new Date(item.starts_at);
  const durationMs = Math.max(60_000, new Date(item.ends_at) - start);
  const dialog = openSheet({
    eyebrow: item.label,
    title: t('plan.moveWork'),
    body: `<label class="field"><span>${esc(t('plan.newStart'))}</span>
        <input type="datetime-local" data-plan-start value="${esc(localInputValue(start))}">
      </label>
      <p class="help">${esc(t('plan.moveKeepsDuration', { d: fmtDuration(durationMs / 60000) }))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-preview-move>${esc(t('plan.preview'))}</button>`,
  });
  dialog.querySelector('[data-preview-move]').addEventListener('click', () => {
    const instant = isoFromLocalInput(dialog.querySelector('[data-plan-start]').value);
    if (!instant) return;
    const end = new Date(new Date(instant).getTime() + durationMs).toISOString();
    dialog.close('preview');
    apply?.(instant, end);
  });
}

export function shiftItem(item, minutes) {
  const delta = Number(minutes) * 60_000;
  return [
    new Date(new Date(item.starts_at).getTime() + delta).toISOString(),
    new Date(new Date(item.ends_at).getTime() + delta).toISOString(),
  ];
}

export function controlSummary(item, startsAt = item.starts_at, endsAt = item.ends_at) {
  return `${item.label}: ${fmtDateTime(startsAt)} — ${fmtDateTime(endsAt)}`;
}
