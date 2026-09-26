// The preview → confirmation → execution step for commands on things the user has
// ("готово эссе", "перенеси созвон на 19:00", "напомни про эссе через час").
//
// A command comes either from the device's own grammar (commands.js — works offline)
// or from the server (the account's language model or the server parser). Nothing
// runs until the user presses the button; closing, cancelling and archiving are
// labelled as such. Device commands become ordinary queued operations (offline, with
// Undo); server proposals are applied through /assistant/apply, which re-checks the
// confirmation, the item's version and the action's validity on the server.
import { api } from './api.js';
import { peek } from './store.js';
import { t, fmtDateTime, fmtDuration } from './i18n.js';
import { esc, icon, setBusy } from './ui.js';
import { change, mutate } from './actions.js';
import { matchTarget, rescheduleChange } from './commands.js';
import { syncDeviceAlarms } from './reminders.js';

const DESTRUCTIVE = new Set(['COMPLETE_OBLIGATION', 'CANCEL_OBLIGATION', 'ARCHIVE_OBLIGATION']);
const KINDS = {
  COMPLETE_OBLIGATION: ['TASK', 'REMINDER'], CANCEL_OBLIGATION: ['TASK', 'EVENT', 'REMINDER'], ARCHIVE_OBLIGATION: ['TASK'],
  RESCHEDULE: ['TASK', 'EVENT', 'REMINDER'], SNOOZE: ['TASK', 'REMINDER'], LOG_PROGRESS: ['TASK'],
  UPDATE_TASK: ['TASK'], UPDATE_EVENT: ['EVENT'], UPDATE_REMINDER: ['REMINDER'], REFINE_TASK: ['TASK'],
};

// Everything the device knows, in the shape the command grammar matches against.
export function knownItems() {
  const tasks = peek('/api/v1/tasks') || peek('/api/v1/today')?.tasks || [];
  const events = peek('/api/v1/events') || peek('/api/v1/today')?.plan?.canonical_events || [];
  const reminders = peek('/api/v1/reminders') || [];
  return [
    ...tasks.map((x) => ({ ...x, kind: 'TASK' })),
    ...events.map((x) => ({ ...x, kind: 'EVENT' })),
    ...reminders.map((x) => ({ ...x, kind: 'REMINDER' })),
  ];
}

export const isCommand = (action) => Boolean(KINDS[action?.command]);

function targetOf(action, picks, index) {
  if (picks[index]) return picks[index];
  const id = action.payload?.reminder_id || action.payload?.obligation_id;
  const unresolved = (action.unresolved_fields || []).some((f) => ['target', 'obligation_id', 'reminder_id'].includes(f));
  if (!id || unresolved) return null;
  return knownItems().find((x) => x.id === id && (action.payload.reminder_id ? x.kind === 'REMINDER' : x.kind !== 'REMINDER')) || null;
}

// The words the user used for the item (the server parser keeps them in obligation_id
// while the target is unresolved).
const spokenTarget = (action) => action.payload?.target_text
  || ((action.unresolved_fields || []).includes('obligation_id') ? action.payload?.obligation_id : '') || '';

// "на завтра, 18:00": a moment inside a sentence starts lower-case.
const when = (value) => { const text = fmtDateTime(value); return text.charAt(0).toLowerCase() + text.slice(1); };

function describe(action, item) {
  const p = action.payload || {};
  const title = item?.title || spokenTarget(action) || '…';
  switch (action.command) {
    case 'COMPLETE_OBLIGATION': return t(item?.kind === 'REMINDER' ? 'cmd.completeReminder' : 'cmd.complete', { title });
    case 'CANCEL_OBLIGATION': return t(item?.kind === 'EVENT' ? 'cmd.cancelEvent' : item?.kind === 'REMINDER' ? 'cmd.cancelReminder' : 'cmd.cancel', { title });
    case 'ARCHIVE_OBLIGATION': return t('cmd.archive', { title });
    case 'RESCHEDULE': return p.when ? t(p.keep_time ? 'cmd.rescheduleDay' : 'cmd.reschedule', { title, when: when(p.when) }) : t('cmd.rescheduleWhen', { title });
    case 'SNOOZE': return t('cmd.snooze', { title, when: when(p.until) });
    case 'LOG_PROGRESS': return p.count ? t('cmd.progressCount', { title, n: p.count }) : t('cmd.progress', { title, d: fmtDuration(p.minutes) });
    case 'UPDATE_TASK': case 'UPDATE_EVENT': case 'UPDATE_REMINDER': return t('cmd.update', { title });
    case 'REFINE_TASK': return t('cmd.refine', { title, d: fmtDuration(p.estimated_total_effort_minutes) });
    default: return t('cmd.other', { title });
  }
}

// The queued operation for a device command (and its Undo, when one makes sense).
export function operationFor(action, item) {
  const p = action.payload || {};
  const kind = item.kind;
  switch (action.command) {
    case 'COMPLETE_OBLIGATION':
      return kind === 'REMINDER' ? { type: 'reminder.done', undo: ['reminder.reopen', {}] }
        : { type: 'task.complete', undo: ['task.reopen', {}] };
    case 'CANCEL_OBLIGATION':
      if (kind === 'EVENT') return { type: 'event.cancel', undo: ['event.reopen', {}] };
      if (kind === 'REMINDER') return { type: 'reminder.cancel', undo: ['reminder.reopen', {}] };
      return { type: 'task.cancel', undo: ['task.reopen', {}] };
    case 'ARCHIVE_OBLIGATION':
      return { type: 'task.archive', undo: [item.status === 'COMPLETED' ? 'task.unarchive' : 'task.restore', {}] };
    case 'RESCHEDULE': {
      const [type, payload] = rescheduleChange(kind, item, p.when, Boolean(p.keep_time));
      let undo = null;
      if (type === 'event.update') undo = ['event.update', { starts_at: item.starts_at }];
      else if (type === 'reminder.update') undo = ['reminder.update', { remind_at: item.remind_at }];
      else if (type === 'task.update') undo = ['task.update', { actual_cutoff: item.actual_cutoff }];
      return { type, payload, undo };
    }
    case 'SNOOZE': return { type: 'reminder.snooze', payload: { until: p.until } };
    case 'LOG_PROGRESS': return { type: 'task.progress', payload: p.count ? { count: p.count } : { minutes: p.minutes } };
    default: return null;
  }
}

function candidatesFor(action) {
  const items = knownItems();
  const open = ['ACTIVE', 'DRAFT', 'SCHEDULED', 'FIRED'];
  const statuses = action.command === 'ARCHIVE_OBLIGATION' ? null : open;
  const found = matchTarget(spokenTarget(action), items, { kinds: KINDS[action.command], statuses }).candidates;
  return found.length ? found : items.filter((x) => KINDS[action.command].includes(x.kind) && (!statuses || statuses.includes(x.status))).slice(0, 6);
}

// Renders the proposal into `box`; resolves when it was carried out or dismissed.
// state: { source: 'local'|'server', batchId?, actions }
export function renderCommands(box, state, { onDone = () => {} } = {}) {
  const picks = {};
  const draw = () => {
    const rows = state.actions.map((action, index) => {
      const item = targetOf(action, picks, index);
      const choosing = !item && isCommand(action);
      const missingWhen = (action.unresolved_fields || []).includes('when');
      return `<li class="command-row ${DESTRUCTIVE.has(action.command) ? 'destructive' : ''}">
        <span>${esc(describe(action, item))}</span>
        ${choosing ? `<div class="command-pick"><small class="help">${esc(t('cmd.which'))}</small>
          <div class="chip-row">${candidatesFor(action).map((x) => `<button type="button" class="chip-toggle" data-pick="${index}" data-kind="${esc(x.kind)}" data-pid="${esc(x.id)}">${esc(x.title)}</button>`).join('') || `<small class="muted">${esc(t('cmd.nothingFits'))}</small>`}</div></div>` : ''}
        ${missingWhen ? `<small class="help">${esc(t('cmd.whenMissing'))}</small>` : ''}
      </li>`;
    }).join('');
    const ready = state.actions.every((a, i) => !isCommand(a) || (targetOf(a, picks, i) && !(a.unresolved_fields || []).includes('when')));
    const destructive = state.actions.some((a) => DESTRUCTIVE.has(a.command));
    box.innerHTML = `<article class="capture-card command-card">
      <span class="eyebrow">${icon('spark')} ${esc(t('capture.commandTitle'))}</span>
      <ul class="plain command-list">${rows}</ul>
      ${destructive ? `<p class="help">${esc(t('cmd.confirmHelp'))}</p>` : ''}
      <button type="button" class="button ${destructive ? 'danger' : 'primary'} wide" data-run ${ready ? '' : 'disabled'}>
        ${esc(destructive ? t('cmd.confirmDestructive') : t('capture.commandApply'))}</button>
    </article>`;
    box.hidden = false;
    box.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
      picks[Number(b.dataset.pick)] = knownItems().find((x) => x.id === b.dataset.pid && x.kind === b.dataset.kind) || null;
      draw();
    }));
    box.querySelector('[data-run]')?.addEventListener('click', async (e) => {
      const button = e.currentTarget;
      setBusy(button, true);
      const ok = state.source === 'server' ? await applyServer(state, picks) : await applyLocal(state, picks);
      if (ok) onDone(); else setBusy(button, false);
    });
  };
  draw();
}

async function applyLocal(state, picks) {
  let applied = null;
  for (const [index, action] of state.actions.entries()) {
    const item = targetOf(action, picks, index);
    const op = item && operationFor(action, item);
    if (!op) continue;
    applied = await change(op.type, item.id, op.payload || {}, {
      success: t('capture.commandDone'),
      undo: op.undo ? () => change(op.undo[0], item.id, op.undo[1]) : null,
    });
  }
  syncDeviceAlarms();
  return Boolean(applied);
}

async function applyServer(state, picks) {
  const edits = {};
  for (const [index, action] of state.actions.entries()) {
    const item = picks[index];
    if (!item) continue;
    edits[action.id] = { [item.kind === 'REMINDER' ? 'reminder_id' : 'obligation_id']: item.id, expected_version: item.version };
  }
  const body = {
    batch_id: state.batchId, action_ids: state.actions.map((a) => a.id),
    // The user pressed the explicit confirmation button for exactly these actions.
    confirmed_action_ids: state.actions.filter((a) => a.requires_confirmation || DESTRUCTIVE.has(a.command)).map((a) => a.id),
    idempotency_key: `assistant-${state.batchId}-${Object.keys(edits).length}`,
  };
  if (Object.keys(edits).length) body.edits = edits;
  const result = await mutate(() => api('/api/v1/assistant/apply', { method: 'POST', body }), { success: t('capture.commandDone') });
  syncDeviceAlarms();
  return Boolean(result);
}
