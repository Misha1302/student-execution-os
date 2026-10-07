// Queued check-in and reminder-series operations projected onto cached read models
// (schema v31). Pure functions: the server stays the owner; this only shows what the
// user just did, offline included, until the server's answer replaces it.
//
// An occurrence is identified by (template_id, original_recurrence_id). A projected
// outcome never invents more than the operation said: «Принял» offline shows DONE
// with the moment the button was pressed, which is also what the server records.

const pad = (n) => String(n).padStart(2, '0');
const WEEKDAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];

export function localRid(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
}

function parseLocal(value) {
  const [day, time = '00:00'] = String(value).split('T');
  const [y, m, d] = day.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  return new Date(y, m - 1, d, hh || 0, mm || 0, 0, 0);
}

export function parseRule(rule) {
  const parts = Object.fromEntries(String(rule || '').split(';').filter(Boolean).map((x) => x.split('=')));
  return {
    freq: parts.FREQ || 'DAILY',
    interval: Number(parts.INTERVAL || 1),
    count: parts.COUNT ? Number(parts.COUNT) : Infinity,
    until: parts.UNTIL ? parseLocal(parts.UNTIL) : null,
    byday: parts.BYDAY ? parts.BYDAY.split(',').map((code) => WEEKDAYS.indexOf(code)).filter((n) => n >= 0) : null,
  };
}

// Original local starts of a rule within [from, to) in the device's zone (the server
// resolves the series zone; a pending series is shown in the device's own time).
export function expandLocal(template, from, to, limit = 400) {
  const rule = parseRule(template.recurrence_rule);
  const start = parseLocal(template.dtstart_local);
  const end = template.series_end_before_local ? parseLocal(template.series_end_before_local) : null;
  const out = [];
  let seen = 0;
  const push = (candidate) => {
    if (candidate < start) return true;
    if (seen >= rule.count || (rule.until && candidate > rule.until) || (end && candidate >= end) || candidate >= to) return false;
    seen += 1;
    if (candidate >= from) out.push(candidate);
    return out.length < limit;
  };
  if (!rule.byday) {
    const step = rule.freq === 'DAILY' ? rule.interval : 7 * rule.interval;
    for (let i = 0; i < 5000; i += 1) {
      const candidate = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i * step, start.getHours(), start.getMinutes());
      if (!push(candidate)) break;
    }
    return out;
  }
  const mondayOffset = (start.getDay() + 6) % 7;
  for (let week = 0; week < 2000; week += rule.interval) {
    let more = true;
    for (const day of [...rule.byday].sort((a, b) => a - b)) {
      const candidate = new Date(start.getFullYear(), start.getMonth(), start.getDate() - mondayOffset + week * 7 + day,
        start.getHours(), start.getMinutes());
      more = push(candidate);
      if (!more) break;
    }
    if (!more) break;
  }
  return out;
}

function pendingOccurrence(template, local) {
  const rid = localRid(local);
  return {
    kind: 'CHECKIN_OCCURRENCE', template_id: template.id, original_recurrence_id: rid, identity: [template.id, rid],
    title: template.title, checkin_kind: template.checkin_kind, dose_text: template.dose_text || null,
    scheduled_at: local.toISOString(), scheduled_local: rid, moved_to_local: null, window_ends_at: null,
    status: 'PENDING', resolved_by: null, occurred_at: null, acted_at: null, quantity_done: 0,
    target_quantity: template.target_quantity ?? null,
    remaining_quantity: template.target_quantity ?? null, unit: template.unit || null,
    remaining_effort_minutes: template.target_quantity && template.unit_effort_seconds
      ? Math.ceil((template.target_quantity * template.unit_effort_seconds) / 60) : null,
    note: null, reminder_id: null, version: 0, _pending: true,
  };
}

function remaining(occ) {
  if (occ.target_quantity == null) return null;
  return Math.max(0, occ.target_quantity - (occ.quantity_done || 0));
}

const isUser = (occ) => occ.resolved_by === 'USER';

// One queued occurrence operation applied to one occurrence payload.
export function applyCheckinOccurrenceOp(occ, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  const at = item.queued_at || new Date().toISOString();
  const next = { ...occ, _pending: true };
  switch (op.type) {
    case 'checkin.occurrence.done':
      if (occ.status === 'DONE' || occ.status === 'SKIPPED' || occ.status === 'CANCELLED') return occ;
      next.status = 'DONE'; next.resolved_by = 'USER'; next.occurred_at = p.occurred_at || at; next.acted_at = at;
      if (occ.target_quantity != null) next.quantity_done = Math.max(occ.quantity_done || 0, occ.target_quantity);
      break;
    case 'checkin.occurrence.skip':
      if (occ.status === 'SKIPPED' || occ.status === 'DONE' || occ.status === 'CANCELLED') return occ;
      next.status = 'SKIPPED'; next.resolved_by = 'USER'; next.acted_at = at; next.occurred_at = null;
      if (p.note) next.note = p.note;
      break;
    case 'checkin.occurrence.cancel':
      if (occ.status === 'CANCELLED' || isUser(occ)) return occ;
      next.status = 'CANCELLED'; next.resolved_by = 'USER'; next.acted_at = at;
      break;
    case 'checkin.occurrence.reopen':
      if (occ.status === 'PENDING') return occ;
      next.status = 'PENDING'; next.resolved_by = null; next.acted_at = null; next.occurred_at = null;
      break;
    case 'checkin.occurrence.progress': {
      if (occ.status === 'SKIPPED' || occ.status === 'CANCELLED') return occ;
      next.quantity_done = (occ.quantity_done || 0) + Number(p.count || 0);
      if (occ.target_quantity != null && next.quantity_done >= occ.target_quantity && occ.status !== 'DONE') {
        next.status = 'DONE'; next.resolved_by = 'USER'; next.occurred_at = p.occurred_at || at; next.acted_at = at;
      }
      break;
    }
    case 'checkin.occurrence.move': {
      if (occ.status !== 'PENDING' || !p.target_local) return occ;
      const local = parseLocal(p.target_local);
      next.moved_to_local = localRid(local) === occ.original_recurrence_id ? null : localRid(local);
      next.scheduled_local = localRid(local);
      next.scheduled_at = local.toISOString();
      break;
    }
    default:
      return occ;
  }
  next.remaining_quantity = remaining(next);
  return next;
}

const occurrenceOps = (ops) => ops.filter((x) => x.operation?.type?.startsWith('checkin.occurrence.'));
const matches = (occ, item) => {
  const p = item.operation?.payload || {};
  const templateId = p.template_id || item.operation?.entity_id;
  return occ.template_id === templateId && occ.original_recurrence_id === p.original_recurrence_id;
};

export function projectCheckinItems(items, ops) {
  const relevant = occurrenceOps(ops || []);
  if (!relevant.length) return items;
  return (items || []).map((occ) => relevant.reduce((acc, item) => (matches(acc, item) ? applyCheckinOccurrenceOp(acc, item) : acc), occ));
}

function deletedTemplates(ops) {
  return new Set(ops.filter((x) => x.operation?.type === 'checkin.delete').map((x) => x.operation.entity_id));
}

function pendingTemplate(op, at) {
  const p = op.payload || {};
  return {
    kind: 'CHECKIN', id: op.entity_id, checkin_kind: p.kind || 'ROUTINE', title: p.title || '',
    dose_text: p.dose_text || null, instructions: p.instructions || null, target_quantity: p.target_quantity ?? null,
    unit: p.unit || null, unit_effort_seconds: p.unit_effort_seconds ?? null, dtstart_local: p.dtstart_local,
    recurrence_rule: p.recurrence_rule, timezone_name: p.timezone_name, remind: p.remind !== false,
    delivery: p.delivery || 'PUSH', followup_minutes: p.followup_minutes ?? null, window_minutes: p.window_minutes ?? null,
    status: 'ACTIVE', series_end_before_local: null, version: 0, created_at: at, _pending: true,
  };
}

function dayBounds(now) {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return [start, new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1)];
}

function applyTemplateOp(template, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  if (op.type === 'checkin.update') {
    const next = { ...template, _pending: true };
    for (const key of ['title', 'dose_text', 'instructions', 'target_quantity', 'unit', 'unit_effort_seconds', 'remind',
      'delivery', 'followup_minutes', 'window_minutes']) if (key in p) next[key] = p[key];
    return next;
  }
  if (op.type === 'checkin.end') return { ...template, status: 'ENDED', _pending: true };
  return template;
}

// /api/v1/checkins: templates with today's and upcoming occurrences, plus reminder series.
export function projectCheckins(data, ops, now = new Date()) {
  const all = ops || [];
  const deleted = deletedTemplates(all);
  const [todayStart, todayEnd] = dayBounds(now);
  const later = new Date(now.getTime() + 48 * 3600000);
  let checkins = (data?.checkins || []).filter((x) => !deleted.has(x.id));
  for (const item of all) {
    const op = item.operation || {};
    if (op.type === 'checkin.create' && !checkins.some((x) => x.id === op.entity_id) && !deleted.has(op.entity_id)) {
      const template = pendingTemplate(op, item.queued_at);
      template.today = expandLocal(template, todayStart, todayEnd).map((d) => pendingOccurrence(template, d));
      template.upcoming = expandLocal(template, todayEnd, later, 3).map((d) => pendingOccurrence(template, d));
      template.recent_days = [];
      checkins.push(template);
    } else if (op.type === 'checkin.update' || op.type === 'checkin.end') {
      checkins = checkins.map((x) => (x.id === op.entity_id ? applyTemplateOp(x, item) : x));
    }
  }
  checkins = checkins.map((x) => ({
    ...x,
    today: projectCheckinItems(x.status === 'ENDED' && x._pending ? (x.today || []).filter((o) => o.status !== 'PENDING' || new Date(o.scheduled_at) <= now) : x.today || [], all),
    upcoming: x.status === 'ENDED' ? [] : projectCheckinItems(x.upcoming || [], all),
  }));
  return { ...(data || {}), checkins, reminder_series: projectSeries(data?.reminder_series || [], all) };
}

export function projectSeries(list, ops) {
  const gone = new Set(ops.filter((x) => x.operation?.type === 'reminder_series.delete').map((x) => x.operation.entity_id));
  let out = list.filter((x) => !gone.has(x.id));
  for (const item of ops) {
    const op = item.operation || {};
    const p = op.payload || {};
    if (op.type === 'reminder_series.create' && !out.some((x) => x.id === op.entity_id) && !gone.has(op.entity_id)) {
      out.push({ kind: 'REMINDER_SERIES', id: op.entity_id, title: p.title, note: p.note || null,
        dtstart_local: p.dtstart_local, recurrence_rule: p.recurrence_rule, timezone_name: p.timezone_name,
        delivery: p.delivery || 'PUSH', status: 'ACTIVE', version: 0, next: null, _pending: true });
    } else if (op.type === 'reminder_series.update') {
      out = out.map((x) => (x.id === op.entity_id ? { ...x, ...Object.fromEntries(['title', 'note', 'delivery'].filter((k) => k in p).map((k) => [k, p[k]])), _pending: true } : x));
    } else if (op.type === 'reminder_series.end') {
      out = out.map((x) => (x.id === op.entity_id ? { ...x, status: 'ENDED', next: null, _pending: true } : x));
    }
  }
  return out;
}

// /api/v1/today: today's check-ins with queued outcomes and series created offline.
export function projectTodayCheckins(items, ops, now = new Date()) {
  const all = ops || [];
  const deleted = deletedTemplates(all);
  let out = projectCheckinItems((items || []).filter((x) => !deleted.has(x.template_id)), all);
  const [start, end] = dayBounds(now);
  for (const item of all) {
    const op = item.operation || {};
    if (op.type !== 'checkin.create' || out.some((x) => x.template_id === op.entity_id) || deleted.has(op.entity_id)) continue;
    const template = pendingTemplate(op, item.queued_at);
    out = [...out, ...projectCheckinItems(expandLocal(template, start, end).map((d) => pendingOccurrence(template, d)), all)];
  }
  return out.sort((a, b) => String(a.scheduled_at).localeCompare(String(b.scheduled_at)));
}

// /api/v1/checkins/{id}: the history of one check-in.
export function projectCheckinDetail(data, ops) {
  if (!data) return data;
  const all = ops || [];
  if (all.some((x) => x.operation?.type === 'checkin.delete' && x.operation.entity_id === data.id)) return { ...data, deleted: true };
  let out = { ...data };
  for (const item of all) {
    if (item.operation?.entity_id === data.id && (item.operation.type === 'checkin.update' || item.operation.type === 'checkin.end')) {
      out = applyTemplateOp(out, item);
    }
  }
  out.history = (data.history || []).map((day) => {
    const occurrences = projectCheckinItems(day.occurrences || [], all);
    const counted = occurrences.filter((o) => o.status !== 'CANCELLED');
    return { ...day, occurrences, done: counted.filter((o) => o.status === 'DONE').length, total: counted.length,
      open: counted.filter((o) => o.status === 'PENDING').length };
  });
  return out;
}

// A queued skip/move of a series occurrence shown on the cached reminder list.
export function applySeriesOccurrenceToReminders(reminders, ops) {
  const relevant = (ops || []).filter((x) => x.operation?.type?.startsWith('reminder_series.occurrence.'));
  if (!relevant.length) return reminders;
  return (reminders || []).map((r) => relevant.reduce((acc, item) => {
    const p = item.operation.payload || {};
    if (acc.series?.series_id !== item.operation.entity_id || acc.series?.original_recurrence_id !== p.original_recurrence_id) return acc;
    if (item.operation.type === 'reminder_series.occurrence.skip') return { ...acc, status: 'CANCELLED', _pending: true };
    if (item.operation.type === 'reminder_series.occurrence.move' && p.remind_at) {
      return { ...acc, remind_at: p.remind_at, status: 'SCHEDULED', fired_at: null, _pending: true };
    }
    return acc;
  }, r));
}
