const CRITICAL = {
  EVENT: ['starts_at', 'ends_at', 'duration_minutes', 'remind_before_minutes'],
  TASK: ['actual_cutoff', 'actionable_from', 'target_at', 'remind_at'],
  REMINDER: ['remind_at', 'delivery'],
  NOTE: [],
};

function equivalent(first, second, field) {
  if (['starts_at', 'ends_at', 'actionable_from', 'target_at', 'remind_at'].includes(field)) {
    const firstInstant = Date.parse(first), secondInstant = Date.parse(second);
    if (Number.isFinite(firstInstant) && Number.isFinite(secondInstant)) return firstInstant === secondInstant;
  }
  if (field === 'actual_cutoff' && first?.state === 'KNOWN' && second?.state === 'KNOWN') return equivalent(first.at, second.at, 'remind_at');
  return JSON.stringify(first ?? null) === JSON.stringify(second ?? null);
}

export function reconcileCaptureCandidates(local, model, { userKind = null, edits = {} } = {}) {
  let selected = local || model || { kind: userKind || 'TASK', payload: {} };
  const conflicts = [];
  if (local && model && local.kind !== model.kind) {
    if (userKind) selected = local;
    else if (local.confidence < 0.5 && model.confidence >= 0.8) selected = model;
    else conflicts.push({ field: 'kind', local: local.kind, model: model.kind });
  }
  const kind = selected.kind;
  const payload = { ...(selected.kind === kind ? selected.payload : {}) };
  const provenance = Object.fromEntries(Object.keys(payload).map((field) => [field, selected === model ? 'MODEL_EXPLICIT' : 'LOCAL_EXPLICIT']));
  if (model?.kind === kind) {
    for (const [field, value] of Object.entries(model.payload || {})) {
      if (value === undefined || field in edits) continue;
      if (local?.provenance?.[field] === 'USER_TURN') continue;
      if (kind === 'EVENT' && ['starts_at', 'ends_at', 'duration_minutes'].includes(field)
        && local?.provenance?.starts_at === 'USER_TURN' && model.payload.starts_at
        && !equivalent(local.payload.starts_at, model.payload.starts_at, 'starts_at')) continue;
      const explicit = Object.hasOwn(payload, field) && payload[field] != null
        && !(field === 'actual_cutoff' && payload[field].state === 'UNKNOWN')
        && local?.provenance?.[field] !== 'LOCAL_INFERRED';
      if (explicit && (CRITICAL[kind] || []).includes(field) && !equivalent(payload[field], value, field)) {
        conflicts.push({ field, local: payload[field], model: value });
        continue;
      }
      payload[field] = value;
      provenance[field] = 'MODEL_EXPLICIT';
    }
  }
  if (kind === 'EVENT' && conflicts.some((conflict) => ['starts_at', 'ends_at', 'duration_minutes'].includes(conflict.field))) {
    for (const field of ['starts_at', 'ends_at', 'duration_minutes']) {
      if (field in (local?.payload || {})) { payload[field] = local.payload[field]; provenance[field] = 'LOCAL_EXPLICIT'; }
      else delete payload[field];
    }
  }
  for (const [field, value] of Object.entries(edits)) {
    payload[field] = value;
    provenance[field] = 'USER_EDIT';
  }
  if (kind === 'EVENT' && payload.starts_at && payload.ends_at) payload.duration_minutes = Math.round((Date.parse(payload.ends_at) - Date.parse(payload.starts_at)) / 60000);
  return { kind, payload, provenance, conflicts };
}

export class CaptureSession {
  constructor(saved = {}) {
    this.turns = saved.turns || [];
    this.intent = saved.intent || null;
    this.edits = saved.edits || {};
    this.userKind = saved.userKind || null;
    this.revision = 0;
    this.local = saved.local || null;
    this.model = saved.model || null;
    this.correctedKinds = saved.correctedKinds || [];
  }

  input(text, source = 'keyboard', correctedKinds = []) {
    this.revision += 1;
    this.turns.push({ source, text });
    this.turns = this.turns.slice(-20);
    this.model = null;
    this.correctedKinds = source === 'voice' ? [...new Set([...this.correctedKinds, ...correctedKinds])] : [...correctedKinds];
    return this.revision;
  }

  edit(kind, fields) {
    this.edits[kind] = { ...this.edits[kind], ...fields };
  }

  interpret(candidate, source = 'local', revision = this.revision) {
    if (revision !== this.revision) return null;
    if (source === 'local') this.local = candidate;
    else this.model = candidate;
    const result = reconcileCaptureCandidates(this.local, this.model, { userKind: this.userKind,
      edits: this.edits[!this.userKind && this.model && this.local?.confidence < 0.5 && this.model.confidence >= 0.8 ? this.model.kind : this.local?.kind || candidate.kind] || {} });
    this.intent = result;
    return result;
  }

  choose(candidate) {
    this.userKind = candidate.kind;
    this.edit(candidate.kind, candidate.payload);
    this.model = null;
    this.local = candidate;
    return this.interpret(candidate);
  }

  snapshot(raw) {
    return { raw, turns: this.turns, intent: this.intent, edits: this.edits, userKind: this.userKind, local: this.local, model: this.model, correctedKinds: this.correctedKinds };
  }
}

const PREFIX = 'seos.capture-draft.v1:';
const TTL = 7 * 86400000;

export function draftScope(session, origin) {
  const account = session.user?.account_id;
  if (session.authMode === 'session' && !account) return null;
  return `${PREFIX}${encodeURIComponent(session.server || origin)}:${encodeURIComponent(account || 'bound')}`;
}

export function readCaptureDraft(storage, scope, time = Date.now()) {
  if (!scope) return null;
  try {
    const saved = JSON.parse(storage.getItem(scope) || 'null');
    if (!saved || time - saved.savedAt > TTL || saved.savedAt > time) {
      storage.removeItem(scope);
      return null;
    }
    return saved.draft;
  } catch { return null; }
}

export function writeCaptureDraft(storage, scope, draft, time = Date.now()) {
  if (!scope) return;
  try {
    if (!draft?.raw?.trim()) storage.removeItem(scope);
    else storage.setItem(scope, JSON.stringify({ savedAt: time, draft }));
  } catch {}
}

export function clearCaptureDrafts(storage) {
  try {
    for (const key of Object.keys(storage)) if (key.startsWith(PREFIX)) storage.removeItem(key);
  } catch {}
}
