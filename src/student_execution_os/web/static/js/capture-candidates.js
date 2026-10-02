// Interpretation candidates for the capture session: the local parser's and the
// model's reading of the same words as one typed candidate, plus the conversions
// between Task, Event and Reminder drafts when the kind changes. The session
// (capture-session.js) decides which candidate wins; this only builds them.
import { now } from './i18n.js';
import { DEFAULT_LEAD } from './events.js';
import { hasAlarm } from './reminders.js';
import { captureKind } from './nlparse.js';

// A fixed-time event read as a task (the user said "это задача"): the slot becomes
// the work window and its length the effort; nothing is due.
export function taskFromEvent(e) {
  return {
    title: e.title,
    description: e.description ?? null,
    category: e.category || 'GENERAL',
    importance: e.importance || 'NORMAL',
    actionable_from: e.starts_at,
    target_at: e.ends_at,
    estimated_total_effort_minutes: Math.max(5, Math.round((new Date(e.ends_at) - new Date(e.starts_at)) / 60000)),
    actual_cutoff: { state: 'ABSENT' },
  };
}

export function eventFromTask(d) {
  const start = d.actionable_from ? new Date(d.actionable_from) : (() => { const x = now(); x.setMinutes(0, 0, 0); x.setHours(x.getHours() + 1); return x; })();
  const minutes = Number(d.estimated_total_effort_minutes) || 60;
  return {
    title: d.title, description: d.description ?? null, category: d.category || 'GENERAL', importance: d.importance || 'NORMAL',
    starts_at: start.toISOString(), ends_at: new Date(start.getTime() + minutes * 60000).toISOString(),
    attendance_policy: 'REQUIRED', remind_before_minutes: DEFAULT_LEAD,
  };
}

// Presence-aware reminder enrichment. `undefined` means the provider omitted a
// field, not "reset it"; explicit card edits outrank deterministic intent, which
// in turn is a floor under AI enrichment.
export function mergeReminderDraft(previous, incoming, floor = null) {
  const old = previous || {};
  const next = { ...old };
  for (const key of ['title', 'remind_at', 'note', 'delivery', 'wake_check', 'raise_volume', 'obligation_id']) {
    if (key in (incoming || {})) next[key] = incoming[key];
  }
  next.deliveryChosen = Boolean(old.deliveryChosen);
  next.wakeChosen = Boolean(old.wakeChosen);
  next.whenChosen = Boolean(old.whenChosen);
  // A late answer (the assistant) never overwrites what the person set by hand.
  if (old.whenChosen) next.remind_at = old.remind_at;
  if (old.deliveryChosen) next.delivery = old.delivery;
  if (old.wakeChosen) next.wake_check = old.wake_check;
  if (!next.delivery) next.delivery = 'PUSH';
  if (!('wake_check' in next)) next.wake_check = false;
  if (!('raise_volume' in next)) next.raise_volume = hasAlarm(next.delivery);
  if (floor && !next.deliveryChosen) {
    if (floor.delivery === 'PUSH_AND_ALARM' || next.delivery === 'PUSH') next.delivery = floor.delivery;
  }
  if (floor?.wake_check && !next.wakeChosen) next.wake_check = true;
  return next;
}

export const KINDS = ['TASK', 'EVENT', 'REMINDER', 'NOTE'];

export const blankTaskDraft = () => ({ title: '', importance: 'NORMAL', category: 'GENERAL',
  estimated_total_effort_minutes: null, actual_cutoff: { state: 'UNKNOWN' }, splittable: false });

export function localCandidate(parsed, raw, correctedKinds = []) {
  const kind = parsed.kind || captureKind(parsed, raw);
  const payload = { ...parsed };
  delete payload.kind; delete payload.unresolved; delete payload.cutoff_time_assumed;
  delete payload.inferred_fields;
  const provenance = Object.fromEntries((parsed.inferred_fields || []).map((field) => [field, 'LOCAL_INFERRED']));
  if (kind === 'TASK' && payload.actual_cutoff?.state === 'UNKNOWN') {
    payload.actual_cutoff = { state: 'ABSENT' };
    provenance.actual_cutoff = 'LOCAL_INFERRED';
  }
  if (kind === 'REMINDER' && hasAlarm(parsed.delivery)) provenance.delivery = 'USER_TURN';
  if (kind === 'EVENT' && correctedKinds.includes('reminder')) provenance.remind_before_minutes = 'USER_TURN';
  if (correctedKinds.some((field) => ['date', 'time', 'part', 'range', 'instant'].includes(field))) {
    const fields = kind === 'EVENT' ? ['starts_at', 'ends_at', 'duration_minutes'] : kind === 'REMINDER' ? ['remind_at'] : ['actionable_from', 'target_at', 'actual_cutoff'];
    for (const field of fields) if (payload[field] != null && provenance[field] !== 'LOCAL_INFERRED') provenance[field] = 'USER_TURN';
  }
  return { kind, payload, confidence: kind === 'NOTE' ? 0.35 : (parsed.unresolved || []).includes('actual_cutoff') && (parsed.unresolved || []).includes('estimated_total_effort_minutes') ? 0.45 : 0.85,
    provenance, unresolved: parsed.unresolved || [] };
}

export function modelCandidate(action) {
  const kind = { CREATE_TASK: 'TASK', CREATE_EVENT: 'EVENT', CREATE_REMINDER: 'REMINDER', CREATE_NOTE: 'NOTE' }[action?.command];
  return kind ? { kind, payload: { ...(action.payload || {}) }, provenance: 'MODEL_EXPLICIT',
    confidence: action.confidence, unresolved: action.unresolved_fields || [] } : null;
}
