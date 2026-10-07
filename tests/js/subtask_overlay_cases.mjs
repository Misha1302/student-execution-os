import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-10-06T06:00:00Z') => ({ state: 'PENDING', queued_at: at,
  operation: { op_id: `op-${type}-${entity_id}-${at}`, type, entity_id, payload: payload || {} } });
const base = { task_id: 'task-1', subtasks: [
  { id: 'sub-a', task_id: 'task-1', title: 'Введение', position: 1, effort_minutes: null, done: false, done_at: null },
  { id: 'sub-b', task_id: 'task-1', title: 'Выводы', position: 2, effort_minutes: null, done: false, done_at: null }] };

// Offline: add between, tick, move, delete — order by fractional position.
const view = project('/api/v1/tasks/task-1/subtasks', base, [
  op('subtask.create', 'sub-c', { task_id: 'task-1', title: 'План', position: 1.5 }),
  op('subtask.complete', 'sub-a', { task_id: 'task-1', occurred_at: '2026-10-06T05:59:00Z' }, '2026-10-06T06:01:00Z'),
  op('subtask.move', 'sub-b', { task_id: 'task-1', position: 0.5 }, '2026-10-06T06:02:00Z'),
]);
assert.deepEqual(view.subtasks.map((x) => x.title), ['Выводы', 'Введение', 'План']);
assert.equal(view.subtasks[1].done, true);
const deleted = project('/api/v1/tasks/task-1/subtasks', base, [op('subtask.delete', 'sub-a', { task_id: 'task-1' })]);
assert.deepEqual(deleted.subtasks.map((x) => x.id), ['sub-b']);
// Another task's steps never leak in.
const other = project('/api/v1/tasks/task-1/subtasks', base, [op('subtask.create', 'sub-x', { task_id: 'task-2', title: 'Чужой' })]);
assert.equal(other.subtasks.length, 2);
// The task list summary follows steps that carry their task id.
const list = project('/api/v1/tasks', [{ id: 'task-1', kind: 'TASK', title: 'Курсовая', status: 'ACTIVE', checklist: { total: 2, done: 0, percent: 0, basis: 'COUNT' } }],
  [op('subtask.complete', 'sub-a', { task_id: 'task-1' }), op('subtask.create', 'sub-c', { task_id: 'task-1', title: 'План' }, '2026-10-06T06:01:00Z')]);
assert.equal(list[0].checklist.total, 3);
assert.equal(list[0].checklist.done, 1);
console.log('subtask overlay: ok');
