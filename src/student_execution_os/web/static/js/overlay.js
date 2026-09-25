// Local-first view of the read models.
//
// The server stays the only source of truth: cached responses (store.js) are never
// edited. Instead every read is passed through this pure projection of the
// operations the device has queued (sync.js) and of those the server accepted after
// that response was fetched. So a task created, started, finished, put off or
// deleted without network shows up that way at once, on every screen, and after an
// app restart — and disappears from the overlay as soon as a fresh response
// already contains the change.
//
// Plan blocks and risk are the planner's; offline we only hide what no longer
// applies (work for a finished or postponed task) and list new tasks as "not yet
// planned" until the server replans.

const OPEN = new Set(['ACTIVE', 'DRAFT']);

const clone = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));
const minutes = (value) => (value == null || value === '' ? null : Math.max(0, Math.round(Number(value))));

function newTask(id, payload, at) {
  const effort = minutes(payload.estimated_total_effort_minutes);
  const count = payload.count_total ? { total: Number(payload.count_total), done: Number(payload.count_done || 0), unit: payload.count_unit || null } : null;
  return {
    kind: 'TASK',
    id,
    title: String(payload.title || '').trim(),
    description: payload.description ?? null,
    category: payload.category || 'GENERAL',
    importance: payload.importance || 'NORMAL',
    status: effort == null ? 'DRAFT' : 'ACTIVE',
    version: 1,
    created_at: at,
    updated_at: at,
    completed_at: null,
    estimated_total_effort_minutes: effort,
    remaining_effort_minutes: effort,
    splittable: Boolean(payload.splittable),
    min_chunk_minutes: payload.min_chunk_minutes ?? null,
    max_chunk_minutes: payload.max_chunk_minutes ?? null,
    actionable_from: payload.actionable_from ?? null,
    target_at: payload.target_at ?? null,
    started_at: null,
    last_progress_at: null,
    remind_at: payload.remind_at ?? null,
    count_progress: count,
    actual_cutoff: payload.actual_cutoff || { state: 'UNKNOWN', at: null },
    risk: null,
    cutoff_truth: null,
    _pending: true,
  };
}

function newEvent(id, payload) {
  const start = payload.starts_at;
  const end = payload.ends_at;
  return {
    kind: 'EVENT',
    id,
    title: String(payload.title || '').trim(),
    description: payload.description ?? null,
    category: payload.category || 'GENERAL',
    importance: payload.importance || 'NORMAL',
    status: 'ACTIVE',
    version: 1,
    starts_at: start,
    ends_at: end,
    time_semantics: 'FIXED_INTERVAL',
    attendance_policy: payload.attendance_policy || 'REQUIRED',
    location_effect: payload.location_effect || { kind: 'NONE', origin_place_id: null, destination_place_id: null },
    location_options: [],
    selected_location_option_id: null,
    arrival_requirement_minutes: Number(payload.arrival_requirement_minutes || 0),
    duration_minutes: Math.round((new Date(end) - new Date(start)) / 60000),
    remind_before_minutes: payload.remind_before_minutes ?? null,
    remind_at: null,
    canonical: true,
    _pending: true,
  };
}

// One queued operation applied to one task. Returns the new task, or null when the
// task is gone (deleted). Unknown operations leave the task unchanged.
export function applyTaskOp(task, item) {
  const op = item.operation;
  const p = op.payload || {};
  const at = item.queued_at || new Date().toISOString();
  if (op.type === 'task.create') return task || newTask(op.entity_id, p, at);
  if (!task) return task;
  if (op.type === 'task.delete') return null;
  const next = { ...task, updated_at: at, _pending: true };
  switch (op.type) {
    case 'task.update': {
      for (const [key, value] of Object.entries(p)) {
        if (key === 'expected_version' || key.startsWith('count_')) continue;
        next[key] = value;
      }
      if ('estimated_total_effort_minutes' in p && !('remaining_effort_minutes' in p) && task.status === 'DRAFT') {
        next.remaining_effort_minutes = minutes(p.estimated_total_effort_minutes);
      }
      if (next.status === 'DRAFT' && next.estimated_total_effort_minutes != null) next.status = 'ACTIVE';
      if ('count_total' in p) {
        next.count_progress = p.count_total
          ? { total: Number(p.count_total), done: Number(p.count_done ?? task.count_progress?.done ?? 0), unit: p.count_unit ?? task.count_progress?.unit ?? null }
          : null;
      } else if (task.count_progress && ('count_done' in p || 'count_unit' in p)) {
        next.count_progress = { ...task.count_progress, ...(p.count_done != null ? { done: Number(p.count_done) } : {}), ...('count_unit' in p ? { unit: p.count_unit } : {}) };
      }
      return next;
    }
    case 'task.start':
      next.started_at = task.started_at || at;
      next.last_progress_at = at;
      if (next.status === 'DRAFT') next.status = 'ACTIVE';
      return next;
    case 'task.progress': {
      // A progress report the base already contains must not be subtracted twice.
      if (task.last_progress_at && new Date(task.last_progress_at) >= new Date(at)) return task;
      const count = task.count_progress;
      const doneBefore = count ? count.done : 0;
      if (count && p.count) next.count_progress = { ...count, done: Math.min(count.total, count.done + Number(p.count)) };
      if (p.minutes != null && task.remaining_effort_minutes != null) {
        next.remaining_effort_minutes = Math.max(0, task.remaining_effort_minutes - Number(p.minutes));
      } else if (count && p.count && task.remaining_effort_minutes != null) {
        const left = count.total - next.count_progress.done;
        const before = count.total - doneBefore;
        next.remaining_effort_minutes = before ? Math.ceil(task.remaining_effort_minutes * (left / before)) : 0;
      }
      next.started_at = task.started_at || at;
      next.last_progress_at = at;
      return next;
    }
    case 'task.defer':
      next.actionable_from = p.until;
      next.remind_at = p.until;
      return next;
    case 'task.complete':
      next.status = 'COMPLETED';
      next.completed_at = task.completed_at || at;
      return next;
    case 'task.cancel':
      next.status = 'CANCELLED';
      return next;
    case 'task.reopen':
      next.status = task.estimated_total_effort_minutes == null ? 'DRAFT' : 'ACTIVE';
      next.completed_at = null;
      if (task.remaining_effort_minutes === 0) next.remaining_effort_minutes = Math.min(15, task.estimated_total_effort_minutes || 15);
      return next;
    case 'task.archive':
      next.status = 'ARCHIVED';
      return next;
    case 'task.unarchive':
      next.status = task.completed_at ? 'COMPLETED' : 'CANCELLED';
      return next;
    default:
      return task;
  }
}

export function applyEventOp(event, item) {
  const op = item.operation;
  const p = op.payload || {};
  if (op.type === 'event.create') return event || newEvent(op.entity_id, p);
  if (!event) return event;
  if (op.type === 'event.delete') return null;
  const next = { ...event, _pending: true };
  switch (op.type) {
    case 'event.update': {
      for (const [key, value] of Object.entries(p)) if (key !== 'expected_version') next[key] = value;
      if ('starts_at' in p && !('ends_at' in p)) {
        next.ends_at = new Date(new Date(p.starts_at).getTime() + (new Date(event.ends_at) - new Date(event.starts_at))).toISOString();
      }
      next.duration_minutes = Math.round((new Date(next.ends_at) - new Date(next.starts_at)) / 60000);
      return next;
    }
    case 'event.cancel': next.status = 'CANCELLED'; return next;
    case 'event.reopen': next.status = 'ACTIVE'; return next;
    default: return event;
  }
}

// Operations that change what a read model fetched at `fetchedAt` shows: everything
// still queued, and what the server accepted after that fetch started.
export function relevantOps(items, fetchedAt) {
  return (items || []).filter((item) => {
    if (item.state === 'PENDING') return true;
    if (item.state === 'ACKED') return Number(item.acked_at || 0) > Number(fetchedAt || 0);
    return false; // CONFLICT / REJECTED: the server said no, show its truth
  });
}

function projectList(list, ops, prefix, apply) {
  const byId = new Map((list || []).map((item) => [item.id, item]));
  const order = (list || []).map((item) => item.id);
  let changed = false;
  for (const item of ops) {
    const op = item.operation;
    if (!op?.type?.startsWith(prefix)) continue;
    const before = byId.get(op.entity_id) || null;
    const after = apply(before, item);
    if (after === before) continue;
    changed = true;
    if (after === null) byId.delete(op.entity_id);
    else {
      if (!before) order.unshift(op.entity_id);
      byId.set(op.entity_id, after);
    }
  }
  if (!changed) return list;
  return order.filter((id, i) => byId.has(id) && order.indexOf(id) === i).map((id) => byId.get(id));
}

export const projectTasks = (list, ops) => projectList(list, ops, 'task.', applyTaskOp);
export const projectEvents = (list, ops) => projectList(list, ops, 'event.', applyEventOp);

// A task the plan should not currently schedule.
function unschedulable(task, now) {
  return !task || !OPEN.has(task.status) || (task.actionable_from && new Date(task.actionable_from) > now);
}

function projectPlan(plan, tasksById, eventOps, now) {
  if (!plan) return plan;
  const next = { ...plan };
  next.blocks = (plan.blocks || []).filter((b) => b.type !== 'WORK' || !unschedulable(tasksById.get(b.obligation_id), now));
  if (eventOps.length) {
    const events = projectEvents(plan.canonical_events || [], eventOps);
    next.canonical_events = (events || []).filter((e) => e.status === 'ACTIVE');
    const gone = new Set((plan.canonical_events || []).filter((e) => !next.canonical_events.some((x) => x.id === e.id)).map((e) => e.id));
    next.blocks = next.blocks.filter((b) => !(b.type === 'EVENT_PROJECTION' && gone.has(b.source_event_id || b.obligation_id)));
  }
  return next;
}

function taskOps(ops) { return ops.filter((x) => x.operation?.type?.startsWith('task.')); }
function eventOps(ops) { return ops.filter((x) => x.operation?.type?.startsWith('event.')); }

// Today-shaped models: /api/v1/today and /api/v1/plan/agenda.
function projectDay(data, ops, now) {
  const tOps = taskOps(ops);
  const eOps = eventOps(ops);
  const out = { ...data };
  // Today lists active tasks in `tasks` and drafts in `needs_refinement`; a queued
  // change can move a task between the two or out of both.
  const known = [...(data.tasks || []), ...(data.needs_refinement || [])];
  const touched = new Set(tOps.map((x) => x.operation.entity_id));
  const projected = projectTasks(known, tOps) || [];
  const byId = new Map(projected.map((task) => [task.id, task]));
  if (tOps.length) {
    out.tasks = projected.filter((task) => task.status === 'ACTIVE');
    if ('needs_refinement' in data) out.needs_refinement = projected.filter((task) => task.status === 'DRAFT');
    out.next_actions = (data.next_actions || []).filter((a) => !unschedulable(byId.get(a.task_id), now));
    const planned = new Set([...(data.next_actions || []).map((a) => a.task_id),
      ...(data.plan?.blocks || []).filter((b) => b.type === 'WORK').map((b) => b.obligation_id)]);
    // Tasks the device changed that the (stale) plan does not place yet.
    out.unplanned_pending = projected.filter((task) => task.status === 'ACTIVE' && touched.has(task.id) && !planned.has(task.id)).map((task) => task.id);
    if (out.current_action && unschedulable(byId.get(out.current_action.task_id), now)) out.current_action = null;
  }
  out.plan = projectPlan(data.plan, byId.size ? byId : new Map(known.map((task) => [task.id, task])), eOps, now);
  if (ops.length) out.pending_changes = ops.filter((x) => x.state === 'PENDING').length;
  return out;
}

// Entry point used by store.load(). `data` is never modified.
export function project(path, data, items, { fetchedAt = 0, now = new Date() } = {}) {
  const ops = relevantOps(items, fetchedAt);
  if (!ops.length || data == null) return data;
  const route = String(path).split('?')[0];
  const base = clone(data);
  if (route === '/api/v1/tasks') return projectTasks(base, taskOps(ops));
  if (route === '/api/v1/events') return projectEvents(base, eventOps(ops));
  if (route === '/api/v1/today' || route === '/api/v1/plan/agenda') return projectDay(base, ops, now);
  if (route === '/api/v1/calendar') return { ...base, events: projectEvents(base.events || [], eventOps(ops)) };
  if (route === '/api/v1/notifications' && Array.isArray(base)) {
    // A reminder answered from the app (or its notification) shows as answered.
    const acted = { 'task.start': 'START', 'task.complete': 'DONE', 'reminder.snooze': 'SNOOZE', 'task.defer': 'RESCHEDULE' };
    return base.map((n) => {
      const hit = ops.find((x) => x.operation?.payload?.reminder_message_id === n.id);
      return hit && !n.acted_at ? { ...n, acted_at: hit.queued_at, acted_action: acted[hit.operation.type] || 'SEEN' } : n;
    });
  }
  return data;
}
