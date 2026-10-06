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
import { t, fmtDateTime, fmtDuration, fmtTime } from './i18n.js';
import { describePreference } from './preferences.js';
import { esc, icon, setBusy } from './ui.js';
import { change, mutate } from './actions.js';
import { matchTarget, rescheduleChange } from './commands.js';
import { syncDeviceAlarms } from './reminders.js';
import { assistantSession, offerUndo } from './assistant-turn.js';
import { ruleLabel, firstStart } from './checkins.js';
import { cachedPlaces, placeName } from './places.js';
import { newEntityId } from './sync.js';

const DESTRUCTIVE = new Set(['COMPLETE_OBLIGATION', 'CANCEL_OBLIGATION', 'ARCHIVE_OBLIGATION']);
const KINDS = {
  COMPLETE_OBLIGATION: ['TASK', 'REMINDER'], CANCEL_OBLIGATION: ['TASK', 'EVENT', 'REMINDER'], ARCHIVE_OBLIGATION: ['TASK'],
  RESCHEDULE: ['TASK', 'EVENT', 'REMINDER'], SNOOZE: ['TASK', 'REMINDER'], LOG_PROGRESS: ['TASK'],
  UPDATE_TASK: ['TASK'], UPDATE_EVENT: ['EVENT'], UPDATE_REMINDER: ['REMINDER'], REFINE_TASK: ['TASK'],
  CHECKIN_OUTCOME: ['CHECKIN'], CHECKIN_PROGRESS: ['CHECKIN'], MOVE_CHECKIN_OCCURRENCE: ['CHECKIN'],
};
// Recurring creations: a check-in (outcome recorded) or a reminder series (attention only).
const RECURRING = new Set(['CREATE_CHECKIN', 'CREATE_REMINDER_SERIES']);

// Everything the device knows, in the shape the command grammar matches against.
export function knownItems() {
  const tasks = peek('/api/v1/tasks') || peek('/api/v1/today')?.tasks || [];
  const events = peek('/api/v1/events') || peek('/api/v1/today')?.plan?.canonical_events || [];
  const reminders = peek('/api/v1/reminders') || [];
  const checkins = peek('/api/v1/checkins')?.checkins || [];
  return [
    ...tasks.map((x) => ({ ...x, kind: 'TASK' })),
    ...events.map((x) => ({ ...x, kind: 'EVENT' })),
    ...reminders.map((x) => ({ ...x, kind: 'REMINDER' })),
    ...checkins.filter((x) => x.status === 'ACTIVE').map((x) => ({ ...x, kind: 'CHECKIN' })),
  ];
}

export const isCommand = (action) => Boolean(KINDS[action?.command]);

// Server actions that only exist as part of an Assistant proposal (never a capture card).
const PLACE_COMMANDS = new Set(['CREATE_PLACE', 'CREATE_LOCATION_TRIGGER']);
const PLAN_ONLY = new Set(['UNDO_LAST', 'CREATE_TIME_CONSTRAINT', 'CREATE_PLANNING_PREFERENCE', ...RECURRING, ...PLACE_COMMANDS]);
// What the server can revert for [Отменить] (it stores an inverse for these).
const REVERSIBLE = new Set(['RESCHEDULE', 'SNOOZE', 'UPDATE_TASK', 'UPDATE_EVENT', 'UPDATE_REMINDER',
  'CREATE_TASK', 'CREATE_EVENT', 'CREATE_REMINDER', 'CREATE_NOTE', 'CREATE_TIME_CONSTRAINT',
  'CREATE_PLANNING_PREFERENCE', 'CREATE_CHECKIN', 'CREATE_REMINDER_SERIES', 'CREATE_PLACE', 'CREATE_LOCATION_TRIGGER']);
// A time the server derives from an earlier action («после неё») — known once that
// action's item is picked, so it does not block the button.
const DERIVED = new Set(['starts_at', 'ends_at', 'actionable_from', 'remind_at']);

// A server answer that needs the command card rather than a single capture card:
// anything about existing items, a plan of several actions, undo, or a constraint.
export const isAssistantPlan = (actions) => actions.length > 1
  || actions.some((a) => isCommand(a) || PLAN_ONLY.has(a.command));

// Overlaps the server found for a new event time (other events, classes, protected
// time, other items of this plan): a warning before confirming, not a refusal.
function conflictsHtml(action) {
  const list = (action.conflicts || []).map((c) => `${c.title || t('cmd.protectedTime')} (${fmtTime(c.starts_at)}–${fmtTime(c.ends_at)})`);
  return list.length ? `<small class="help warn" data-conflicts>${esc(t('cmd.overlap', { list: list.join(', ') }))}</small>` : '';
}

const blocking = (action) => (action.unresolved_fields || [])
  .filter((f) => !(action.payload?.relative_to && DERIVED.has(f)));

function targetOf(action, picks, index) {
  if (picks[index]) return picks[index];
  const id = action.payload?.reminder_id || action.payload?.obligation_id || action.payload?.checkin_id;
  const unresolved = (action.unresolved_fields || []).some((f) => ['target', 'obligation_id', 'reminder_id', 'checkin_id'].includes(f));
  if (!id || unresolved) return null;
  const kindOf = action.payload.reminder_id ? 'REMINDER' : action.payload.checkin_id ? 'CHECKIN' : null;
  return knownItems().find((x) => x.id === id && (kindOf ? x.kind === kindOf : !['REMINDER', 'CHECKIN'].includes(x.kind)))
    || (kindOf === 'CHECKIN' ? { id, kind: 'CHECKIN', title: action.resolution?.title || action.payload.target_text || '', version: action.expected_version } : null);
}

// The words the user used for the item (the server parser keeps them in obligation_id
// while the target is unresolved).
const spokenTarget = (action) => action.payload?.target_text
  || ((action.unresolved_fields || []).includes('obligation_id') ? action.payload?.obligation_id : '') || '';

// "на завтра, 18:00": a moment inside a sentence starts lower-case.
const when = (value) => { const text = fmtDateTime(value); return text.charAt(0).toLowerCase() + text.slice(1); };

const approx = (action, value) => (action.resolution?.precision === 'APPROXIMATE'
  ? t('cmd.approximateTime', { when: when(value) }) : when(value));

// The item's current time, for "было → станет".
const currentTime = (item) => item?.starts_at || item?.remind_at
  || (item?.actual_cutoff?.state === 'KNOWN' ? item.actual_cutoff.at : null);

function describe(action, item) {
  const p = action.payload || {};
  const title = item?.title || spokenTarget(action) || p.title || '…';
  switch (action.command) {
    case 'CREATE_TASK': return p.actionable_from ? t('cmd.createTaskFrom', { title, when: approx(action, p.actionable_from) })
      : t('cmd.createTask', { title });
    case 'CREATE_EVENT': return p.starts_at ? t('cmd.createEvent', { title, when: approx(action, p.starts_at), d: fmtDuration(p.duration_minutes) })
      : t('cmd.createEventWhen', { title });
    case 'CREATE_REMINDER': return p.remind_at ? t('cmd.createReminder', { title, when: approx(action, p.remind_at) })
      : t('cmd.createEventWhen', { title });
    case 'CREATE_NOTE': return t('cmd.createNote');
    case 'CREATE_CHECKIN': return t(`cmd.createCheckin.${p.kind || 'ROUTINE'}`, { title, rule: ruleLabel(p.recurrence_rule),
      time: p.dtstart_local ? String(p.dtstart_local).slice(11, 16) : '…', n: p.target_quantity, unit: p.unit || '', dose: p.dose_text ? ` (${p.dose_text})` : '' });
    case 'CREATE_REMINDER_SERIES': return t('cmd.createSeries', { title, rule: ruleLabel(p.recurrence_rule),
      time: p.dtstart_local ? String(p.dtstart_local).slice(11, 16) : '…' });
    case 'CHECKIN_OUTCOME': {
      const at = action.resolution?.scheduled_at ? fmtTime(action.resolution.scheduled_at) : '';
      return t(p.outcome === 'SKIPPED' ? 'cmd.checkinSkipped' : action.resolution?.checkin_kind === 'MEDICATION' ? 'cmd.checkinTaken' : 'cmd.checkinDone', { title, time: at });
    }
    case 'CHECKIN_PROGRESS': return t('cmd.checkinProgress', { title, n: p.count });
    case 'CREATE_PLACE': return p.address ? t('cmd.createPlaceAddress', { name: p.display_name, address: p.address }) : t('cmd.createPlace', { name: p.display_name });
    case 'CREATE_LOCATION_TRIGGER': {
      const place = cachedPlaces().find((x) => x.id === p.place_id);
      return t(p.transition === 'EXIT' ? 'cmd.triggerExit' : 'cmd.triggerEnter', { title: p.title, place: place ? placeName(place) : (p.place_text || '…') });
    }
    case 'MOVE_CHECKIN_OCCURRENCE': return t('cmd.checkinMove', { title, from: action.resolution?.scheduled_at ? fmtTime(action.resolution.scheduled_at) : '',
      to: (p.target_local || action.resolution?.target_local || '').slice(11, 16) || fmtTime(p.when) });
    case 'CREATE_TIME_CONSTRAINT': return t(p.type === 'FIXED_PERSONAL_BLOCK' ? 'cmd.constraintBlock' : 'cmd.constraintFree',
      { from: when(p.starts_at), to: when(p.ends_at) });
    case 'CREATE_PLANNING_PREFERENCE': return t('cmd.preference', { what: describePreference(p) });
    case 'UNDO_LAST': return t('cmd.undoLast');
    case 'COMPLETE_OBLIGATION': return t(item?.kind === 'REMINDER' ? 'cmd.completeReminder' : 'cmd.complete', { title });
    case 'CANCEL_OBLIGATION': return t(item?.kind === 'EVENT' ? 'cmd.cancelEvent' : item?.kind === 'REMINDER' ? 'cmd.cancelReminder' : 'cmd.cancel', { title });
    case 'ARCHIVE_OBLIGATION': return t('cmd.archive', { title });
    case 'RESCHEDULE': {
      if (!p.when) return t('cmd.rescheduleWhen', { title });
      const resolved = approx(action, p.when);
      const from = currentTime(item);
      if (from && !p.keep_time) return t('cmd.rescheduleFromTo', { title, from: when(from), when: resolved });
      return t(p.keep_time ? 'cmd.rescheduleDay' : 'cmd.reschedule', { title, when: resolved });
    }
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
    case 'CHECKIN_OUTCOME': case 'CHECKIN_PROGRESS': {
      const occurrence = openOccurrence(item.id, p);
      if (!occurrence) return null;
      const identity = { template_id: item.id, original_recurrence_id: occurrence.original_recurrence_id };
      if (action.command === 'CHECKIN_PROGRESS') return { type: 'checkin.occurrence.progress', payload: { ...identity, count: p.count } };
      return p.outcome === 'SKIPPED'
        ? { type: 'checkin.occurrence.skip', payload: identity, undo: ['checkin.occurrence.reopen', identity] }
        : { type: 'checkin.occurrence.done', payload: { ...identity, occurred_at: new Date().toISOString() }, undo: ['checkin.occurrence.reopen', identity] };
    }
    case 'LOG_PROGRESS': return { type: 'task.progress', payload: p.count ? { count: p.count } : { minutes: p.minutes } };
    default: return null;
  }
}

// Today's open occurrence of a check-in the device already knows (nearest to now, past first),
// the same rule the server applies to Assistant answers.
function openOccurrence(templateId, p) {
  const today = peek('/api/v1/today')?.checkins || (peek('/api/v1/checkins')?.checkins || []).flatMap((c) => c.today || []);
  const parts = { MORNING: [5, 12], AFTERNOON: [12, 17], EVENING: [17, 24], NIGHT: [0, 5] };
  let pool = today.filter((o) => o.template_id === templateId && ['PENDING', 'MISSED'].includes(o.status));
  if (p.day_part && parts[p.day_part]) {
    const [low, high] = parts[p.day_part];
    pool = pool.filter((o) => { const h = Number(String(o.scheduled_local || '').slice(11, 13)); return h >= low && h < high; });
  }
  const now = Date.now();
  const past = pool.filter((o) => new Date(o.scheduled_at).getTime() <= now + 3600000);
  return (past.length ? past : pool).sort((a, b) => Math.abs(new Date(a.scheduled_at) - now) - Math.abs(new Date(b.scheduled_at) - now))[0] || null;
}

function candidatesFor(action) {
  // The server found several items that fit the user's words equally: offer exactly those.
  if (action.target_candidates?.length) return action.target_candidates;
  const items = knownItems();
  const open = ['ACTIVE', 'DRAFT', 'SCHEDULED', 'FIRED'];
  const statuses = action.command === 'ARCHIVE_OBLIGATION' ? null : open;
  const found = matchTarget(spokenTarget(action), items, { kinds: KINDS[action.command], statuses }).candidates;
  return found.length ? found : items.filter((x) => KINDS[action.command].includes(x.kind) && (!statuses || statuses.includes(x.status))).slice(0, 6);
}

// Renders the proposal into `box`; resolves when it was carried out or dismissed.
// state: { source: 'local'|'server', batchId?, validUntil?, actions }
// onRefine: the user wants to correct a server proposal by saying what to change.
const NEW_PLACE = '__new_place__';

export function renderCommands(box, state, { onDone = () => {}, onRefine = null } = {}) {
  const picks = {};
  const times = {};
  const placePicks = {};
  state.placePicks = placePicks;
  // A time the user typed for a recurring creation completes it (the first matching day from now).
  const completed = (action, index) => {
    if (!RECURRING.has(action.command) || !(action.unresolved_fields || []).includes('dtstart_local') || !times[index]) return null;
    const rule = String(action.payload?.recurrence_rule || '');
    const days = (rule.match(/BYDAY=([A-Z,]+)/) || [])[1]?.split(',') || [];
    const choice = days.join(',') === 'MO,TU,WE,TH,FR' ? 'WEEKDAYS' : days.length ? 'DAYS' : 'DAILY';
    return firstStart(times[index], choice, days);
  };
  state.completed = completed;
  const draw = () => {
    const rows = state.actions.map((action, index) => {
      const item = targetOf(action, picks, index);
      const choosing = !item && isCommand(action);
      const missingWhen = (action.unresolved_fields || []).includes('when');
      return `<li class="command-row ${DESTRUCTIVE.has(action.command) ? 'destructive' : ''}">
        <span>${esc(describe(action, item))}</span>
        ${choosing ? `<div class="command-pick"><small class="help">${esc(t('cmd.which'))}</small>
          <div class="chip-row">${candidatesFor(action).map((x) => `<button type="button" class="chip-toggle" data-pick="${index}" data-kind="${esc(x.kind)}" data-pid="${esc(x.id)}">${esc(x.when ? `${x.title} · ${fmtDateTime(x.when)}` : x.title)}</button>`).join('') || `<small class="muted">${esc(t('cmd.nothingFits'))}</small>`}</div></div>` : ''}
        ${missingWhen ? `<small class="help">${esc(t('cmd.whenMissing'))}</small>` : ''}
        ${action.command === 'CREATE_LOCATION_TRIGGER' && (action.unresolved_fields || []).includes('place_id') ? `<div class="command-pick"><small class="help">${esc(t('cmd.whichPlace'))}</small>
          <div class="chip-row">${cachedPlaces().map((x) => `<button type="button" class="chip-toggle ${placePicks[index] === x.id ? 'on' : ''}" data-place-pick="${index}" data-pid="${esc(x.id)}">${esc(placeName(x))}</button>`).join('')}
          ${action.payload?.place_text ? `<button type="button" class="chip-toggle ${placePicks[index] === NEW_PLACE ? 'on' : ''}" data-place-pick="${index}" data-pid="${NEW_PLACE}">${esc(t('cmd.newPlace', { name: action.payload.place_text }))}</button>` : ''}</div></div>` : ''}
        ${RECURRING.has(action.command) && (action.unresolved_fields || []).includes('dtstart_local')
          ? `<label class="field inline"><span>${esc(t('cmd.atTime'))}</span><input type="time" data-recurring-time="${index}" value="${esc(times[index] || '')}"></label>` : ''}
        ${!isCommand(action) && blocking(action).filter((f) => !(RECURRING.has(action.command) && f === 'dtstart_local' && times[index])).length ? `<small class="help">${esc(t('cmd.needsDetails'))}</small>` : ''}
        ${conflictsHtml(action)}
        ${action.blocked ? `<small class="help" data-blocked="${esc(action.blocked.code)}">${esc(t('cmd.sourceOwned'))}</small>` : ''}
      </li>`;
    }).join('');
    // A change the server will refuse (e.g. moving an imported calendar event) is
    // explained, never offered as if it could succeed.
    const ready = state.actions.every((a, i) => !a.blocked && (isCommand(a)
      ? targetOf(a, picks, i) && !(a.unresolved_fields || []).includes('when')
      : !blocking(a).filter((f) => !(f === 'dtstart_local' && completed(a, i)) && !(f === 'place_id' && placePicks[i])).length));
    const destructive = state.actions.some((a) => DESTRUCTIVE.has(a.command));
    box.innerHTML = `<article class="capture-card command-card">
      <span class="eyebrow">${icon('spark')} ${esc(t('capture.commandTitle'))}</span>
      <ul class="plain command-list">${rows}</ul>
      ${destructive ? `<p class="help">${esc(t('cmd.confirmHelp'))}</p>` : ''}
      <button type="button" class="button ${destructive ? 'danger' : 'primary'} wide" data-run ${ready ? '' : 'disabled'}>
        ${esc(destructive ? t('cmd.confirmDestructive') : t('capture.commandApply'))}</button>
      ${onRefine && state.source === 'server' ? `<button type="button" class="button ghost wide" data-refine>${esc(t('cmd.refineRequest'))}</button>` : ''}
    </article>`;
    box.hidden = false;
    box.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
      const index = Number(b.dataset.pick);
      const offered = state.actions[index]?.target_candidates || [];
      picks[index] = knownItems().find((x) => x.id === b.dataset.pid && x.kind === b.dataset.kind)
        || offered.find((x) => x.id === b.dataset.pid && x.kind === b.dataset.kind) || null;
      draw();
    }));
    box.querySelector('[data-refine]')?.addEventListener('click', () => onRefine(state));
    box.querySelectorAll('[data-place-pick]').forEach((b) => b.addEventListener('click', () => {
      placePicks[Number(b.dataset.placePick)] = b.dataset.pid;
      draw();
    }));
    box.querySelectorAll('[data-recurring-time]').forEach((input) => input.addEventListener('change', () => {
      times[Number(input.dataset.recurringTime)] = input.value;
      draw();
    }));
    box.querySelector('[data-run]')?.addEventListener('click', async (e) => {
      const button = e.currentTarget;
      setBusy(button, true);
      // A place the user creates from the card is created on the device first (offline too).
      const local = state.source !== 'server' || Object.values(placePicks).includes(NEW_PLACE);
      const ok = local ? await applyLocal(state, picks) : await applyServer(state, picks);
      if (ok) onDone(); else setBusy(button, false);
    });
  };
  draw();
}

async function applyLocal(state, picks) {
  let applied = null;
  for (const [index, action] of state.actions.entries()) {
    if (action.command === 'CREATE_PLACE') {
      applied = await change('place.create', newEntityId('place'), { ...action.payload },
        { success: t('place.created', { name: action.payload.display_name }) });
      continue;
    }
    if (action.command === 'CREATE_LOCATION_TRIGGER') {
      const payload = { ...action.payload };
      delete payload.place_text;
      const pick = state.placePicks?.[index];
      if (pick === NEW_PLACE) {
        const placeId = newEntityId('place');
        const name = action.payload.place_text.slice(0, 1).toUpperCase() + action.payload.place_text.slice(1);
        if (!(await change('place.create', placeId, { display_name: name }))) return false;
        payload.place_id = placeId;
      } else if (pick) payload.place_id = pick;
      if (!payload.place_id) continue;
      applied = await change('location_trigger.create', newEntityId('trigger'), payload, { success: t(payload.transition === 'EXIT'
        ? 'trigger.createdExit' : 'trigger.createdEnter', { name: placeName(cachedPlaces().find((x) => x.id === payload.place_id)) || action.payload.place_text }) });
      continue;
    }
    if (RECURRING.has(action.command)) {
      const payload = { ...action.payload };
      const start = state.completed?.(action, index);
      if (start) payload.dtstart_local = start;
      if (!payload.dtstart_local) continue;
      const series = action.command === 'CREATE_REMINDER_SERIES';
      applied = await change(series ? 'reminder_series.create' : 'checkin.create', newEntityId(series ? 'series' : 'checkin'), payload, {
        success: t(series ? 'checkin.toast.seriesCreated' : 'checkin.toast.created', { title: payload.title, rule: ruleLabel(payload.recurrence_rule) }),
      });
      continue;
    }
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
    const start = state.completed?.(action, index);
    if (start) edits[action.id] = { dtstart_local: start };
    const placePick = state.placePicks?.[index];
    if (placePick && placePick !== NEW_PLACE) edits[action.id] = { ...(edits[action.id] || {}), place_id: placePick };
    const item = picks[index];
    if (!item) continue;
    const key = item.kind === 'REMINDER' ? 'reminder_id' : item.kind === 'CHECKIN' ? 'checkin_id' : 'obligation_id';
    edits[action.id] = { ...(edits[action.id] || {}), [key]: item.id, expected_version: item.version };
  }
  const body = {
    batch_id: state.batchId, action_ids: state.actions.map((a) => a.id),
    // The user pressed the explicit confirmation button for exactly these actions.
    confirmed_action_ids: state.actions.filter((a) => a.requires_confirmation || DESTRUCTIVE.has(a.command)).map((a) => a.id),
    idempotency_key: `assistant-${state.batchId}-${Object.keys(edits).length}`,
  };
  if (Object.keys(edits).length) body.edits = edits;
  const reversible = state.actions.some((a) => REVERSIBLE.has(a.command));
  const result = await mutate(() => api('/api/v1/assistant/apply', { method: 'POST', body }),
    { success: reversible ? null : t('capture.commandDone') });
  syncDeviceAlarms();
  if (!result) return false;
  // «И напомни за полчаса» right after can refer to what was just done.
  assistantSession.commit(state.batchId, state.validUntil);
  if (reversible) offerUndo(body.idempotency_key);
  return true;
}
