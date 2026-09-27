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
    case 'task.restore':
      if (OPEN.has(task.status) || task.status === 'COMPLETED') return task;
      if (task.status === 'ARCHIVED' && task.completed_at) { next.status = 'COMPLETED'; return next; }
      return applyTaskOp(task, { ...item, operation: { ...op, type: 'task.reopen' } });
    default:
      return task;
  }
}

function executionTaskId(item) {
  const op = item.operation || {};
  return op.type === 'execution.start' || op.type === 'execution.finish' ? op.payload?.task_id : null;
}

function applyExecutionTaskOp(task, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  if (!task || executionTaskId(item) !== task.id) return task;
  const at = p.occurred_at || item.queued_at || new Date().toISOString();
  const next = { ...task, updated_at: at, _pending: true };
  if (op.type === 'execution.start') {
    next.started_at = task.started_at || at;
    next.last_progress_at = at;
    return next;
  }
  if (op.type === 'execution.finish') {
    next.last_progress_at = at;
    if (p.outcome === 'COMPLETE') {
      next.status = 'COMPLETED';
      next.completed_at = task.completed_at || at;
    } else if (p.outcome === 'UPDATE_REMAINING') {
      next.remaining_effort_minutes = Math.max(0, Number(p.remaining_effort_minutes || 0));
      if (next.remaining_effort_minutes > Number(next.estimated_total_effort_minutes || 0)) {
        next.estimated_total_effort_minutes = next.remaining_effort_minutes;
      }
      next.remaining_effort_low_minutes = null;
      next.remaining_effort_high_minutes = null;
    }
    return next;
  }
  return task;
}

function segmentElapsed(session, at) {
  const base = Number(session?.actual_work_seconds || 0);
  if (!session?.current_segment_started_at) return base;
  const measuredAt = session.measured_at || session.current_segment_started_at;
  const extra = Math.max(0, Math.floor((new Date(at) - new Date(measuredAt)) / 1000));
  return base + extra;
}

export function projectExecution(session, ops) {
  let current = session ? { ...session } : null;
  for (const item of ops) {
    const op = item.operation || {};
    const at = op.payload?.occurred_at || item.queued_at || new Date().toISOString();
    if (!op.type?.startsWith('execution.')) continue;
    if (op.type === 'execution.start') {
      if (current && current.id !== op.entity_id) continue;
      if (!current) current = {
        id: op.entity_id, task_id: op.payload?.task_id, task_title: null, state: 'ACTIVE',
        started_at: at, finished_at: null, planning_snapshot_id: op.payload?.planning_snapshot_id || null,
        source_plan_block_id: op.payload?.source_plan_block_id || null, actual_work_seconds: 0,
        actual_work_minutes: 0, measured_at: at, current_segment_started_at: at,
        created_at: at, updated_at: at, version: 1, _pending: true,
      };
      continue;
    }
    if (!current || current.id !== op.entity_id) continue;
    if (op.type === 'execution.pause' && current.state === 'ACTIVE') {
      const seconds = segmentElapsed(current, at);
      current = { ...current, state: 'PAUSED', actual_work_seconds: seconds, actual_work_minutes: Math.floor(seconds / 60), measured_at: at, current_segment_started_at: null, updated_at: at, _pending: true };
    } else if (op.type === 'execution.resume' && current.state === 'PAUSED') {
      current = { ...current, state: 'ACTIVE', measured_at: at, current_segment_started_at: at, updated_at: at, _pending: true };
    } else if (op.type === 'execution.finish' || op.type === 'execution.cancel') {
      current = null;
    }
  }
  return current;
}

function newConstraint(id, payload) {
  return {
    id,
    type: payload.type,
    starts_at: payload.starts_at,
    ends_at: payload.ends_at,
    obligation_id: payload.task_id || null,
    reason: payload.reason || null,
    version: 1,
    ownership: 'CANONICAL',
    _pending: true,
  };
}

export function applyConstraintOp(constraint, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  if (op.type === 'constraint.create') return constraint || newConstraint(op.entity_id, p);
  if (!constraint) return constraint;
  if (op.type === 'constraint.delete') return null;
  if (op.type !== 'constraint.update') return constraint;
  const next = { ...constraint, _pending: true };
  if ('starts_at' in p) {
    const oldDuration = new Date(constraint.ends_at) - new Date(constraint.starts_at);
    next.starts_at = p.starts_at;
    if (!('ends_at' in p)) next.ends_at = new Date(new Date(p.starts_at).getTime() + oldDuration).toISOString();
  }
  if ('ends_at' in p) next.ends_at = p.ends_at;
  if ('reason' in p) next.reason = p.reason;
  next.version = Number(constraint.version || 1) + 1;
  return next;
}

export const projectConstraints = (list, ops) => projectList(list, ops, 'constraint.', applyConstraintOp);

function pendingProject(id, payload, at) {
  return {
    id,
    title: String(payload.title || '').trim(),
    description: payload.description || null,
    status: 'ACTIVE',
    importance: payload.importance || null,
    version: 1,
    created_at: at,
    updated_at: at,
    members: [],
    milestones: [],
    progress: { percent: 0, basis: 'EMPTY', tasks_total: 0, tasks_completed: 0,
      estimated_total_effort_minutes: null, remaining_effort_minutes: null },
    risk: null,
    _pending: true,
  };
}

function recomputeProject(project) {
  const members = project.members || [];
  const tasks = members.filter((x) => x.kind === 'TASK' && x.status !== 'CANCELLED');
  const known = tasks.filter((x) => x.estimated_total_effort_minutes != null);
  const total = known.reduce((sum, x) => sum + Number(x.estimated_total_effort_minutes || 0), 0);
  const remaining = known.filter((x) => !['COMPLETED', 'CANCELLED', 'ARCHIVED'].includes(x.status))
    .reduce((sum, x) => sum + Number(x.remaining_effort_minutes || 0), 0);
  let percent = 0;
  let basis = 'EMPTY';
  if (total > 0) { percent = Math.max(0, Math.min(100, Math.round((total - remaining) * 100 / total))); basis = 'EFFORT'; }
  else if (tasks.length) { percent = Math.round(tasks.filter((x) => x.status === 'COMPLETED').length * 100 / tasks.length); basis = 'TASK_COUNT'; }
  return { ...project, progress: {
    percent, basis, tasks_total: tasks.length,
    tasks_completed: tasks.filter((x) => x.status === 'COMPLETED').length,
    estimated_total_effort_minutes: total || null,
    remaining_effort_minutes: total ? remaining : null,
  }};
}

function pendingProjectTask(id, payload, at) {
  const effort = payload.estimated_total_effort_minutes == null ? null : Number(payload.estimated_total_effort_minutes);
  return {
    kind: 'TASK', id, title: String(payload.title || '').trim(), description: payload.description || null,
    category: payload.category || 'GENERAL', importance: payload.importance || 'NORMAL',
    status: effort == null ? 'DRAFT' : 'ACTIVE', version: 1,
    estimated_total_effort_minutes: effort, remaining_effort_minutes: effort,
    splittable: Boolean(payload.splittable), min_chunk_minutes: payload.min_chunk_minutes || null,
    max_chunk_minutes: payload.max_chunk_minutes || null, actionable_from: payload.actionable_from || null,
    target_at: payload.target_at || null, actual_cutoff: payload.actual_cutoff || { state: 'UNKNOWN' },
    created_at: at, completed_at: null, risk: null, _pending: true,
  };
}

function pendingMilestone(id, payload) {
  return {
    id, title: String(payload.title || '').trim(), marker_at: payload.marker_at,
    role: payload.role || 'INTERMEDIATE', consequence: payload.consequence || null,
    hard_for_planning: Boolean(payload.hard_for_planning), status: 'ACTIVE', version: 1,
    overdue: false, _pending: true,
  };
}

function applyProjectOp(project, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  const at = item.queued_at || new Date().toISOString();
  if (op.type === 'project.create') return project || pendingProject(op.entity_id, p, at);
  if (!project) return project;
  let next = { ...project, members: [...(project.members || [])], milestones: [...(project.milestones || [])], _pending: true, updated_at: at };
  if (op.type === 'project.update') {
    for (const key of ['title', 'description', 'importance']) if (key in p) next[key] = p[key];
    next.version = Number(next.version || 1) + 1;
  } else if (op.type === 'project.complete') next.status = 'COMPLETED';
  else if (op.type === 'project.cancel') next.status = 'CANCELLED';
  else if (op.type === 'project.reopen') next.status = 'ACTIVE';
  else if (op.type === 'project.task.create') {
    const task = pendingProjectTask(p.task_id, p, at);
    if (!next.members.some((x) => x.id === task.id)) next.members.push(task);
    next.version = Number(next.version || 1) + 1;
  } else if (op.type === 'project.member.remove') {
    next.members = next.members.filter((x) => x.id !== p.obligation_id);
    next.version = Number(next.version || 1) + 1;
  }
  return recomputeProject(next);
}

function applyMilestoneToProjects(projects, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  let changed = false;
  const out = projects.map((project) => {
    let milestones = [...(project.milestones || [])];
    const index = milestones.findIndex((m) => m.id === op.entity_id);
    let belongs = index >= 0;
    if (op.type === 'milestone.create') belongs = p.project_id === project.id;
    if (!belongs) return project;
    changed = true;
    if (op.type === 'milestone.create' && index < 0) milestones.push(pendingMilestone(op.entity_id, p));
    else if (op.type === 'milestone.delete') milestones = milestones.filter((m) => m.id !== op.entity_id);
    else if (index >= 0) {
      const current = { ...milestones[index], _pending: true };
      if (op.type === 'milestone.update') {
        if ('title' in p) current.title = p.title;
        if ('marker_at' in p) current.marker_at = p.marker_at;
        current.version = Number(current.version || 1) + 1;
      } else if (op.type === 'milestone.complete') current.status = 'COMPLETED';
      else if (op.type === 'milestone.cancel') current.status = 'CANCELLED';
      else if (op.type === 'milestone.reopen') current.status = 'ACTIVE';
      milestones[index] = current;
    }
    return { ...project, milestones, _pending: true };
  });
  return changed ? out : projects;
}

export function projectProjects(list, ops) {
  let projected = projectList(list, ops, 'project.', applyProjectOp) || [];
  for (const item of ops) {
    if (item.operation?.type?.startsWith('milestone.')) projected = applyMilestoneToProjects(projected, item);
  }
  const routineTaskOps = ops.map(routineTaskProjection).filter(Boolean);
  const taskRelated = [...ops.filter((x) => x.operation?.type?.startsWith('task.') || executionTaskId(x)), ...routineTaskOps];
  if (taskRelated.length) {
    projected = projected.map((project) => {
      let changed = false;
      const members = (project.members || []).map((member) => {
        if (member.kind !== 'TASK') return member;
        let value = member;
        for (const item of taskRelated) {
          const before = value;
          value = item.operation?.type?.startsWith('task.') ? applyTaskOp(value, item) : applyExecutionTaskOp(value, item);
          if (value !== before) changed = true;
        }
        return value;
      }).filter(Boolean);
      return changed ? recomputeProject({ ...project, members, _pending: true }) : project;
    });
  }
  return projected;
}

function routineTaskProjection(item) {
  const op = item.operation || {};
  const p = op.payload || {};
  const taskId = p.task_id;
  if (!taskId || !op.type?.startsWith('routine.occurrence.')) return null;
  if (op.type === 'routine.occurrence.skip') {
    return { ...item, operation: { ...op, type: 'task.cancel', entity_id: taskId, payload: {} } };
  }
  if (op.type === 'routine.occurrence.reopen') {
    return { ...item, operation: { ...op, type: 'task.reopen', entity_id: taskId, payload: {} } };
  }
  if (op.type === 'routine.occurrence.edit') {
    const payload = {};
    if ('title' in p) payload.title = p.title;
    if ('effort_minutes' in p) {
      payload.estimated_total_effort_minutes = p.effort_minutes;
      payload.remaining_effort_minutes = p.effort_minutes;
    }
    return { ...item, operation: { ...op, type: 'task.update', entity_id: taskId, payload } };
  }
  return null;
}

function pendingRoutine(id, payload, at) {
  return {
    id,
    title: String(payload.title || '').trim(),
    description: payload.description || null,
    category: payload.category || 'GENERAL',
    importance: payload.importance || 'NORMAL',
    dtstart_local: payload.dtstart_local,
    effort_minutes: Number(payload.effort_minutes || 0),
    recurrence_rule: payload.recurrence_rule || 'FREQ=WEEKLY',
    timezone_name: payload.timezone_name || 'UTC',
    splittable: Boolean(payload.splittable),
    min_chunk_minutes: payload.min_chunk_minutes ?? null,
    max_chunk_minutes: payload.max_chunk_minutes ?? null,
    status: 'ACTIVE',
    version: 1,
    created_at: at,
    updated_at: at,
    occurrences: [],
    _pending: true,
  };
}

function applyRoutineTemplateOp(routine, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  const at = item.queued_at || new Date().toISOString();
  if (op.type === 'routine.create') return routine || pendingRoutine(op.entity_id, p, at);
  if (!routine) return routine;
  if (op.type === 'routine.cancel') {
    return { ...routine, status: 'CANCELLED', version: Number(routine.version || 1) + 1, updated_at: at, _pending: true };
  }
  return routine;
}

function applyRoutineOccurrenceToTemplates(routines, item) {
  const op = item.operation || {};
  const p = op.payload || {};
  if (!op.type?.startsWith('routine.occurrence.')) return routines;
  let changed = false;
  const next = routines.map((routine) => {
    if (routine.id !== p.template_id) return routine;
    const occurrences = [...(routine.occurrences || [])];
    const index = occurrences.findIndex((o) => o.original_recurrence_id === p.original_recurrence_id);
    if (index < 0) return routine;
    const current = { ...occurrences[index], _pending: true };
    if (op.type === 'routine.occurrence.skip') {
      current.state = 'SKIPPED';
      current.task_status = 'CANCELLED';
    } else if (op.type === 'routine.occurrence.reopen') {
      current.state = 'ACTIVE';
      current.task_status = current.effort_minutes == null ? 'DRAFT' : 'ACTIVE';
    } else if (op.type === 'routine.occurrence.edit') {
      if ('title' in p) current.title = p.title;
      if ('effort_minutes' in p) {
        current.effort_minutes = Number(p.effort_minutes);
        current.remaining_effort_minutes = Number(p.effort_minutes);
      }
      if ('target_local' in p) current.override_target_local = p.target_local;
    }
    current.version = Number(current.version || 1) + 1;
    occurrences[index] = current;
    changed = true;
    return { ...routine, occurrences, _pending: true };
  });
  return changed ? next : routines;
}

export function projectWorkRoutines(data, ops) {
  const source = data?.routines || [];
  let routines = projectList(source, ops, 'routine.', applyRoutineTemplateOp) || [];
  for (const item of ops) routines = applyRoutineOccurrenceToTemplates(routines, item);
  return { ...(data || {}), routines };
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

function newReminder(id, payload, at) {
  const alarm = payload.delivery === 'ALARM' || payload.delivery === 'PUSH_AND_ALARM';
  return {
    kind: 'REMINDER',
    id,
    title: String(payload.title || '').trim(),
    note: payload.note ?? null,
    remind_at: payload.remind_at,
    delivery: payload.delivery || 'PUSH',
    wake_check: alarm && Boolean(payload.wake_check),
    raise_volume: alarm && Boolean(payload.raise_volume),
    obligation_id: payload.obligation_id ?? null,
    status: 'SCHEDULED',
    fired_at: null,
    acknowledged_at: null,
    awake_confirmed_at: null,
    completed_at: null,
    snooze_count: 0,
    created_at: at,
    updated_at: at,
    version: 1,
    _pending: true,
  };
}

// One queued operation applied to one standalone reminder (see reminders/standalone.py).
export function applyReminderOp(reminder, item) {
  const op = item.operation;
  const p = op.payload || {};
  const at = item.queued_at || new Date().toISOString();
  if (op.type === 'reminder.create') return reminder || newReminder(op.entity_id, p, at);
  if (!reminder) return reminder;
  if (op.type === 'reminder.delete') return null;
  const open = reminder.status === 'SCHEDULED' || reminder.status === 'FIRED';
  const next = { ...reminder, updated_at: at, _pending: true };
  switch (op.type) {
    case 'reminder.update':
      for (const key of ['title', 'note', 'delivery', 'wake_check', 'raise_volume']) if (key in p) next[key] = p[key];
      if (next.delivery === 'PUSH') { next.wake_check = false; next.raise_volume = false; }
      if ('remind_at' in p) Object.assign(next, { remind_at: p.remind_at, status: 'SCHEDULED', fired_at: null, acknowledged_at: null, completed_at: null });
      return next;
    case 'reminder.snooze': {
      if (!open) return reminder;
      const until = p.until || new Date(new Date(at).getTime() + Number(p.minutes || 0) * 60000).toISOString();
      return { ...next, remind_at: until, status: 'SCHEDULED', fired_at: null, acknowledged_at: null, snooze_count: (reminder.snooze_count || 0) + 1 };
    }
    case 'reminder.done':
      return open ? { ...next, status: 'DONE', completed_at: at } : reminder;
    case 'reminder.ack':
      if (!open) return reminder;
      if (p.stage === 'AWAKE' || !reminder.wake_check) return { ...next, status: 'DONE', acknowledged_at: reminder.acknowledged_at || at, completed_at: at };
      return { ...next, status: 'FIRED', acknowledged_at: reminder.acknowledged_at || at };
    case 'reminder.cancel':
      return { ...next, status: 'CANCELLED' };
    case 'reminder.reopen':
      return open ? reminder : { ...next, status: 'SCHEDULED', completed_at: null };
    default:
      return reminder;
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

export function projectTasks(list, ops) {
  const routineTaskOps = (ops || []).map(routineTaskProjection).filter(Boolean);
  const allTaskOps = [...(ops || []), ...routineTaskOps];
  let projected = projectList(list, allTaskOps, 'task.', applyTaskOp);
  const execution = (ops || []).filter((x) => executionTaskId(x));
  if (!execution.length) return projected;
  projected = projected || [];
  return projected.map((task) => execution.reduce((value, item) => applyExecutionTaskOp(value, item), task));
}
export const projectEvents = (list, ops) => projectList(list, ops, 'event.', applyEventOp);
// reminder.snooze also snoozes a task's reminder: only ids already known as standalone
// reminders (or created as one) are projected here.
export function projectReminders(list, ops) {
  const known = new Set((list || []).map((x) => x.id));
  for (const x of ops) if (x.operation?.type === 'reminder.create') known.add(x.operation.entity_id);
  return projectList(list, ops.filter((x) => known.has(x.operation?.entity_id)), 'reminder.', applyReminderOp);
}

// A task the plan should not currently schedule.
function unschedulable(task, now) {
  return !task || !OPEN.has(task.status) || (task.actionable_from && new Date(task.actionable_from) > now);
}

function projectPlan(plan, tasksById, eventOps, constraints, now) {
  if (!plan) return plan;
  const next = { ...plan };
  next.blocks = (plan.blocks || []).filter((b) => b.type !== 'WORK' || !unschedulable(tasksById.get(b.obligation_id), now));
  if (constraints.length) {
    next.constraints = projectConstraints(plan.constraints || [], constraints);
    // A queued plan-control mutation changes planner inputs. Do not pretend the stale
    // derived WORK witness is still authoritative while offline; canonical facts stay.
    next.blocks = next.blocks.filter((b) => b.type !== 'WORK');
    next.pending_control = true;
  }
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
function executionOps(ops) { return ops.filter((x) => x.operation?.type?.startsWith('execution.')); }
function constraintOps(ops) { return ops.filter((x) => x.operation?.type?.startsWith('constraint.')); }

// Today-shaped models: /api/v1/today and /api/v1/plan/agenda.
function projectDay(data, ops, now) {
  const tOps = taskOps(ops);
  const eOps = eventOps(ops);
  const xOps = executionOps(ops);
  const cOps = constraintOps(ops);
  const out = { ...data };
  out.active_execution = projectExecution(data.active_execution || null, xOps);
  // Today lists active tasks in `tasks` and drafts in `needs_refinement`; a queued
  // change can move a task between the two or out of both.
  const known = [...(data.tasks || []), ...(data.needs_refinement || [])];
  const touched = new Set([...tOps.map((x) => x.operation.entity_id), ...xOps.map(executionTaskId).filter(Boolean)]);
  const projected = projectTasks(known, ops) || [];
  const byId = new Map(projected.map((task) => [task.id, task]));
  if (tOps.length || xOps.length) {
    out.tasks = projected.filter((task) => task.status === 'ACTIVE');
    if ('needs_refinement' in data) out.needs_refinement = projected.filter((task) => task.status === 'DRAFT');
    out.next_actions = (data.next_actions || []).filter((a) => !unschedulable(byId.get(a.task_id), now));
    const planned = new Set([...(data.next_actions || []).map((a) => a.task_id),
      ...(data.plan?.blocks || []).filter((b) => b.type === 'WORK').map((b) => b.obligation_id)]);
    // Tasks the device changed that the (stale) plan does not place yet.
    out.unplanned_pending = projected.filter((task) => task.status === 'ACTIVE' && touched.has(task.id) && !planned.has(task.id)).map((task) => task.id);
    if (out.current_action && unschedulable(byId.get(out.current_action.task_id), now)) out.current_action = null;
  }
  out.plan = projectPlan(data.plan, byId.size ? byId : new Map(known.map((task) => [task.id, task])), eOps, cOps, now);
  if (ops.length) out.pending_changes = ops.filter((x) => x.state === 'PENDING').length;
  return out;
}

// Entry point used by store.load(). `data` is never modified.
export function project(path, data, items, { fetchedAt = 0, now = new Date() } = {}) {
  const ops = relevantOps(items, fetchedAt);
  if (!ops.length || data == null) return data;
  const route = String(path).split('?')[0];
  const base = clone(data);
  if (route === '/api/v1/tasks') return projectTasks(base, ops);
  if (route === '/api/v1/events') return projectEvents(base, eventOps(ops));
  if (route === '/api/v1/today' || route === '/api/v1/plan/agenda') return projectDay(base, ops, now);
  if (route === '/api/v1/calendar') return { ...base, events: projectEvents(base.events || [], eventOps(ops)) };
  if (route === '/api/v1/reminders') return projectReminders(base, ops);
  if (route === '/api/v1/plan/constraints') return projectConstraints(base, constraintOps(ops));
  if (route === '/api/v1/work-routines') return projectWorkRoutines(base, ops);
  if (route === '/api/v1/projects') return projectProjects(base, ops);
  if (route.startsWith('/api/v1/projects/')) {
    const projected = projectProjects(base ? [base] : [], ops);
    return projected[0] || base;
  }
  if (route === '/api/v1/execution/active') return { ...base, session: projectExecution(base.session || null, executionOps(ops)) };
  if (route === '/api/v1/notifications' && Array.isArray(base)) {
    // A reminder answered from the app (or its notification) shows as answered.
    const acted = { 'task.start': 'START', 'task.complete': 'DONE', 'reminder.snooze': 'SNOOZE', 'task.defer': 'RESCHEDULE',
      'reminder.done': 'DONE', 'reminder.ack': 'DONE' };
    return base.map((n) => {
      const hit = ops.find((x) => x.operation?.payload?.reminder_message_id === n.id);
      return hit && !n.acted_at ? { ...n, acted_at: hit.queued_at, acted_action: acted[hit.operation.type] || 'SEEN' } : n;
    });
  }
  return data;
}
