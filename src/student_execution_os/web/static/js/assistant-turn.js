// One Assistant turn beyond a single capture card: conversation continuity, answers to
// questions, and the [Отменить] for an applied server proposal.
//
// Continuity is deliberately small. The server keeps the semantic state (the previous
// batch, its actions and the user's edits); the device only remembers which batch the
// next utterance follows up on, in memory, for one account, for a few minutes. A turn
// becomes "the previous one" only when the user commits to it — applies it or asks to
// refine it — never while they are still typing, so half-typed text is never context.
import { api, session } from './api.js';
import { mutate } from './actions.js';
import { t, fmtDateTime, fmtDuration, fmtTime } from './i18n.js';
import { esc, icon, toast } from './ui.js';

const FOLLOW_UP_MS = 5 * 60 * 1000;
const EXPIRY_MARGIN_MS = 60 * 1000;

let previous = null; // { batchId, scope, until }

const scope = () => `${session.server || 'same-origin'}|${session.user?.account_id || ''}`;

export const assistantSession = {
  // The batch the user just applied or wants to refine becomes the follow-up context.
  // validUntil is device time (see validUntil()), so a skewed phone clock cannot
  // send an expired batch or drop a live one.
  commit(batchId, validUntil) {
    if (!batchId) return;
    previous = { batchId, scope: scope(), until: Math.min(Date.now() + FOLLOW_UP_MS, validUntil ?? Infinity) };
  },
  forget() { previous = null; },
  // Extra interpret context for the next utterance ({} when there is nothing to follow).
  context() {
    if (!previous || previous.scope !== scope() || Date.now() >= previous.until) { previous = null; return {}; }
    return { previous_batch_id: previous.batchId };
  },
  get active() { return Boolean(this.context().previous_batch_id); },
};

// When a just-received batch stops being usable, in device time: the server's own
// lifetime for it (expires_at − created_at), counted from now, minus a margin.
export function validUntil(result) {
  const lifetime = Date.parse(result?.expires_at || '') - Date.parse(result?.created_at || '');
  return Number.isFinite(lifetime) ? Date.now() + lifetime - EXPIRY_MARGIN_MS : Date.now();
}

// [Отменить] for a server-applied proposal. It names the apply it belongs to, so a
// late tap after a newer Assistant change is refused instead of undoing that one.
export function offerUndo(applyKey) {
  if (!applyKey) return;
  const run = () => mutate(() => api('/api/v1/assistant/undo', {
    method: 'POST', body: { idempotency_key: `undo-${applyKey}`, apply_idempotency_key: applyKey },
  }), { success: t('assistant.undone') });
  toast(t('capture.commandDone'), { action: { label: t('common.undo'), run }, duration: 10000 });
}

const at = (value) => (value ? fmtDateTime(value) : '');

function itemRow(item) {
  const when = item.starts_at ? `${at(item.starts_at)}${item.ends_at ? `–${fmtTime(item.ends_at)}` : ''}`
    : item.actual_cutoff_at ? t('assistant.read.due', { when: at(item.actual_cutoff_at) })
      : item.at ? at(item.at) : item.remind_at ? at(item.remind_at) : '';
  return `<li><strong>${esc(item.title || '…')}</strong>${when ? ` <small class="muted">${esc(when)}</small>` : ''}</li>`;
}

const list = (items) => (items?.length
  ? `<ul class="plain read-list">${items.map(itemRow).join('')}</ul>`
  : `<p class="muted">${esc(t('assistant.read.nothing'))}</p>`);

// Only server facts are shown — never the model's prose — so an answer cannot invent
// an event or a planner reason.
function factsHtml(read) {
  const facts = read?.facts;
  switch (read?.query?.kind) {
    case 'AGENDA_WINDOW': case 'DUE_BEFORE': case 'URGENT_TASKS':
      return list(facts);
    case 'FREE_TIME':
      return `<p>${esc(t('assistant.read.free', {
        free: fmtDuration(facts.calendar_free_minutes), total: fmtDuration(facts.window_minutes),
      }))}</p><small class="help">${esc(t('assistant.read.freeScope'))}</small>`;
    case 'ITEM_LOOKUP':
      return list(facts ? [facts] : []);
    case 'WHAT_NOW':
      return facts ? list([{ title: facts.title || t('assistant.read.planBlock'), starts_at: facts.starts_at, ends_at: facts.ends_at }])
        : `<p class="muted">${esc(t('assistant.read.nothingPlanned'))}</p>`;
    case 'PLAN_EXPLANATION':
      if (!facts?.scheduled) return `<p class="muted">${esc(t('assistant.read.notScheduled'))}</p>`;
      return `<p>${esc(t('assistant.read.scheduledAt', { when: at(facts.starts_at) }))}</p>
        ${typeof facts.explanation === 'string' && facts.explanation ? `<p class="help">${esc(facts.explanation)}</p>` : ''}
        ${facts.feasibility_status && facts.feasibility_status !== 'FEASIBLE'
          ? `<p class="help">${esc(t(`assistant.read.feasibility.${facts.feasibility_status}`))}</p>` : ''}`;
    default:
      return `<p class="muted">${esc(t('assistant.read.nothing'))}</p>`;
  }
}

export function renderRead(box, read) {
  box.innerHTML = `<article class="capture-card read-card" data-read="${esc(read?.query?.kind || '')}">
    <span class="eyebrow">${icon('spark')} ${esc(t('assistant.read.title'))}</span>
    ${factsHtml(read)}
  </article>`;
  box.hidden = false;
}
