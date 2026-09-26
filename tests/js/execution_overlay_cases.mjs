import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const task = {
  id: 'task-execution-1',
  title: 'LLVM',
  status: 'ACTIVE',
  version: 1,
  estimated_total_effort_minutes: 120,
  remaining_effort_minutes: 120,
  started_at: null,
  last_progress_at: null,
};

const item = (type, entity_id, payload, queued_at) => ({
  state: 'PENDING',
  queued_at,
  operation: { op_id: 'op-' + type + '-' + queued_at, type, entity_id, payload: payload || {} },
});

const base = {
  tasks: [task],
  needs_refinement: [],
  next_actions: [],
  plan: { blocks: [], canonical_events: [] },
  active_execution: null,
};

const start = item('execution.start', 'execution-1', { task_id: task.id }, '2026-09-27T08:00:00Z');
let day = project('/api/v1/today', base, [start], { now: new Date('2026-09-27T08:10:00Z') });
assert.equal(day.active_execution.state, 'ACTIVE');
assert.equal(day.active_execution.task_id, task.id);
assert.equal(day.tasks[0].remaining_effort_minutes, 120, 'starting work is not progress');

const pause = item('execution.pause', 'execution-1', {}, '2026-09-27T08:30:00Z');
day = project('/api/v1/today', base, [start, pause], { now: new Date('2026-09-27T08:40:00Z') });
assert.equal(day.active_execution.state, 'PAUSED');
assert.equal(day.active_execution.actual_work_seconds, 30 * 60);

const resume = item('execution.resume', 'execution-1', {}, '2026-09-27T09:00:00Z');
const keep = item('execution.finish', 'execution-1', { task_id: task.id, outcome: 'KEEP_REMAINING' }, '2026-09-27T09:45:00Z');
day = project('/api/v1/today', base, [start, pause, resume, keep], { now: new Date('2026-09-27T09:45:00Z') });
assert.equal(day.active_execution, null);
assert.equal(day.tasks[0].remaining_effort_minutes, 120, 'actual work never auto-decrements remaining effort');

const update = item('execution.finish', 'execution-1', {
  task_id: task.id, outcome: 'UPDATE_REMAINING', remaining_effort_minutes: 45,
}, '2026-09-27T09:45:00Z');
day = project('/api/v1/today', base, [start, update], { now: new Date('2026-09-27T09:45:00Z') });
assert.equal(day.tasks[0].remaining_effort_minutes, 45);

const complete = item('execution.finish', 'execution-1', { task_id: task.id, outcome: 'COMPLETE' }, '2026-09-27T09:45:00Z');
day = project('/api/v1/today', base, [start, complete], { now: new Date('2026-09-27T09:45:00Z') });
assert.equal(day.tasks[0].status, 'COMPLETED');

console.log('execution overlay: ok');
