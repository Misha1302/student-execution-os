import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const task = {
  id: 'task-plan-control',
  title: 'Architecture review',
  status: 'ACTIVE',
  estimated_total_effort_minutes: 60,
  remaining_effort_minutes: 60,
};

const base = {
  tasks: [task],
  needs_refinement: [],
  next_actions: [],
  active_execution: null,
  plan: {
    feasibility_status: 'FEASIBLE',
    constraints: [],
    canonical_events: [],
    off_hours: [],
    blocks: [{
      id: 'work-1', type: 'WORK', obligation_id: task.id,
      starts_at: '2026-09-28T10:00:00Z', ends_at: '2026-09-28T11:00:00Z',
      source_constraint_ids: [],
    }],
  },
};

const op = (type, id, payload, at = '2026-09-28T09:00:00Z') => ({
  state: 'PENDING',
  queued_at: at,
  operation: { op_id: 'op-' + type + '-' + id, type, entity_id: id, payload },
});

const create = op('constraint.create', 'constraint-pin-1', {
  type: 'PINNED_WORK', task_id: task.id,
  starts_at: '2026-09-28T13:00:00Z', ends_at: '2026-09-28T14:00:00Z',
  reason: 'USER_PINNED_PLAN_WORK',
});
let day = project('/api/v1/today', base, [create], { now: new Date('2026-09-28T09:00:00Z') });
assert.equal(day.plan.pending_control, true);
assert.equal(day.plan.blocks.filter((x) => x.type === 'WORK').length, 0, 'stale derived work must be hidden');
assert.equal(day.plan.constraints.length, 1);
assert.equal(day.plan.constraints[0].obligation_id, task.id);

const update = op('constraint.update', 'constraint-pin-1', {
  expected_version: 1, starts_at: '2026-09-28T14:00:00Z', ends_at: '2026-09-28T15:00:00Z',
});
let constraints = project('/api/v1/plan/constraints', [], [create, update]);
assert.equal(constraints[0].starts_at, '2026-09-28T14:00:00Z');
assert.equal(constraints[0].version, 2);

const remove = op('constraint.delete', 'constraint-pin-1', { expected_version: 2 });
constraints = project('/api/v1/plan/constraints', [], [create, update, remove]);
assert.equal(constraints.length, 0);

console.log('plan control overlay: ok');
