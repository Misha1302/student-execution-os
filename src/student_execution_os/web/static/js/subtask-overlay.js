// Queued checklist operations on the cached task checklist and task summaries (schema v33).
// Pure; the server stays the owner. Order is the fractional position, then the id.

const isSubtaskOp = (x) => (x.operation?.type || '').startsWith('subtask.');
const order = (a, b) => (a.position - b.position) || String(a.id).localeCompare(String(b.id));

function summary(items) {
  if (!items.length) return null;
  const done = items.filter((x) => x.done);
  const byEffort = items.every((x) => x.effort_minutes != null);
  const total = byEffort ? items.reduce((s, x) => s + x.effort_minutes, 0) : items.length;
  const doneValue = byEffort ? done.reduce((s, x) => s + x.effort_minutes, 0) : done.length;
  return { total: items.length, done: done.length, percent: total ? Math.round((100 * doneValue) / total) : 0,
    basis: byEffort ? 'EFFORT' : 'COUNT', effort_minutes: byEffort ? total : null,
    effort_left_minutes: byEffort ? items.filter((x) => !x.done).reduce((s, x) => s + x.effort_minutes, 0) : null };
}

export function applySubtaskOps(taskId, items, ops) {
  let out = [...(items || [])];
  for (const item of (ops || []).filter(isSubtaskOp)) {
    const op = item.operation;
    const p = op.payload || {};
    const at = item.queued_at || new Date().toISOString();
    if (op.type === 'subtask.create') {
      if (p.task_id !== taskId || out.some((x) => x.id === op.entity_id)) continue;
      const last = out.reduce((m, x) => Math.max(m, x.position), 0);
      out.push({ kind: 'SUBTASK', id: op.entity_id, task_id: taskId, title: p.title, position: p.position ?? last + 1,
        effort_minutes: p.effort_minutes ?? null, done_at: null, done: false, version: 0, _pending: true });
      continue;
    }
    out = out.flatMap((x) => {
      if (x.id !== op.entity_id) return [x];
      switch (op.type) {
        case 'subtask.delete': return [];
        case 'subtask.complete': return [x.done ? x : { ...x, done: true, done_at: p.occurred_at || at, _pending: true }];
        case 'subtask.reopen': return [x.done ? { ...x, done: false, done_at: null, _pending: true } : x];
        case 'subtask.move': return [{ ...x, position: p.position, _pending: true }];
        case 'subtask.update': return [{ ...x, ...('title' in p ? { title: p.title } : {}), ...('effort_minutes' in p ? { effort_minutes: p.effort_minutes } : {}), _pending: true }];
        default: return [x];
      }
    });
  }
  return out.sort(order);
}

// /api/v1/tasks/{id}/subtasks
export function projectSubtasks(data, ops) {
  if (!data) return data;
  return { ...data, subtasks: applySubtaskOps(data.task_id, data.subtasks || [], ops) };
}

// Task lists: a checklist summary follows queued steps that carry their task_id.
export function projectChecklistSummaries(tasks, ops) {
  const relevant = (ops || []).filter((x) => isSubtaskOp(x) && x.operation.payload?.task_id);
  if (!relevant.length || !Array.isArray(tasks)) return tasks;
  return tasks.map((task) => {
    const mine = relevant.filter((x) => x.operation.payload.task_id === task.id);
    if (!mine.length || !task.checklist && !mine.some((x) => x.operation.type === 'subtask.create')) return task;
    const base = task.checklist || { total: 0, done: 0 };
    let { total, done } = base;
    for (const item of mine) {
      const type = item.operation.type;
      if (type === 'subtask.create') total += 1;
      else if (type === 'subtask.delete') total = Math.max(0, total - 1);
      else if (type === 'subtask.complete') done = Math.min(total, done + 1);
      else if (type === 'subtask.reopen') done = Math.max(0, done - 1);
    }
    return { ...task, checklist: total ? { ...base, total, done, percent: Math.round((100 * done) / total), basis: 'COUNT', _pending: true } : null };
  });
}

export { summary as checklistSummary };
