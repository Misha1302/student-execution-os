import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-09-30T09:00:00Z') => ({
  state: 'PENDING',
  queued_at: at,
  operation: { op_id: 'op-' + type + '-' + entity_id, type, entity_id, payload: payload || {} },
});

const task = {
  id: 'routine-task-1',
  kind: 'TASK',
  title: 'Algorithms practice',
  status: 'ACTIVE',
  version: 1,
  estimated_total_effort_minutes: 45,
  remaining_effort_minutes: 45,
  actual_cutoff: { state: 'ABSENT', at: null },
  target_at: '2026-10-01T18:00:00Z',
};

const baseRoutine = {
  now: '2026-09-30T09:00:00Z',
  routines: [{
    id: 'routine-1',
    title: 'Algorithms practice',
    status: 'ACTIVE',
    version: 1,
    effort_minutes: 45,
    recurrence_rule: 'FREQ=DAILY',
    timezone_name: 'UTC',
    occurrences: [{
      template_id: 'routine-1',
      original_recurrence_id: '2026-10-01T18:00:00',
      identity: ['routine-1', '2026-10-01T18:00:00'],
      task_id: task.id,
      state: 'ACTIVE',
      title: task.title,
      target_at: task.target_at,
      effort_minutes: 45,
      remaining_effort_minutes: 45,
      task_status: 'ACTIVE',
      version: 1,
    }],
  }],
};

const skip = op('routine.occurrence.skip', task.id, {
  template_id: 'routine-1',
  original_recurrence_id: '2026-10-01T18:00:00',
  task_id: task.id,
});
let routines = project('/api/v1/work-routines', baseRoutine, [skip]);
assert.equal(routines.routines[0].occurrences[0].state, 'SKIPPED');
assert.equal(routines.routines[0].occurrences[0].task_status, 'CANCELLED');

let tasks = project('/api/v1/tasks', [task], [skip]);
assert.equal(tasks[0].status, 'CANCELLED', 'routine skip must project onto canonical task');

const reopen = op('routine.occurrence.reopen', task.id, {
  template_id: 'routine-1',
  original_recurrence_id: '2026-10-01T18:00:00',
  task_id: task.id,
});
tasks = project('/api/v1/tasks', [task], [skip, reopen]);
assert.equal(tasks[0].status, 'ACTIVE');

const edit = op('routine.occurrence.edit', task.id, {
  template_id: 'routine-1',
  original_recurrence_id: '2026-10-01T18:00:00',
  task_id: task.id,
  title: 'Hard algorithms practice',
  effort_minutes: 60,
  target_local: '2026-10-01T20:00',
});
tasks = project('/api/v1/tasks', [task], [edit]);
assert.equal(tasks[0].title, 'Hard algorithms practice');
assert.equal(tasks[0].remaining_effort_minutes, 60);

const create = op('routine.create', 'routine-new-1', {
  title: 'Read papers',
  effort_minutes: 30,
  recurrence_rule: 'FREQ=WEEKLY',
  timezone_name: 'UTC',
  dtstart_local: '2026-10-02T19:00',
});
routines = project('/api/v1/work-routines', { now: baseRoutine.now, routines: [] }, [create]);
assert.equal(routines.routines[0].title, 'Read papers');
assert.equal(routines.routines[0].occurrences.length, 0, 'offline client must not invent materialized tasks');

console.log('work routines overlay: ok');
