import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-10-01T08:00:00Z') => ({
  state: 'PENDING',
  queued_at: at,
  operation: { op_id: 'op-' + type + '-' + entity_id, type, entity_id, payload },
});

const today = {
  now: '2026-10-01T08:00:00Z',
  local_date: '2026-10-01',
  daily_intent: null,
  tasks: [{ id: 'task-a', kind: 'TASK', title: 'A', status: 'ACTIVE', version: 1,
    estimated_total_effort_minutes: 60, remaining_effort_minutes: 60 }],
  needs_refinement: [],
  next_actions: [],
  active_execution: null,
  plan: { blocks: [{ type: 'WORK', obligation_id: 'task-a', starts_at: '2026-10-01T09:00:00Z', ends_at: '2026-10-01T10:00:00Z' }], canonical_events: [], constraints: [] },
};

const setIntent = op('intent.set', '2026-10-01', {
  local_date: '2026-10-01',
  priority_task_ids: ['task-a'],
  note: 'Focus',
});
let projected = project('/api/v1/today', today, [setIntent]);
assert.deepEqual(projected.daily_intent.priority_task_ids, ['task-a']);
assert.equal(projected.daily_intent.note, 'Focus');
assert.equal(projected.daily_intent._pending, true);
assert.equal(projected.plan.blocks.filter((x) => x.type === 'WORK').length, 0, 'stale planner output must be hidden after offline intent change');
assert.equal(projected.plan.pending_preferences, true);

const close = op('intent.close', '2026-10-01', { local_date: '2026-10-01' });
projected = project('/api/v1/today', today, [setIntent, close]);
assert.ok(projected.daily_intent.closed_at);

const reflection = {
  local_date: '2026-10-01',
  daily_intent: null,
  calibration: [{
    category: 'HOMEWORK', sample_size: 5, median_ratio: 1.5, mean_ratio: 1.6,
    median_percent_difference: 50, suggested_multiplier: 1.5, preference: null,
  }],
};
const calibration = op('calibration.set', 'calibration-HOMEWORK', {
  category: 'HOMEWORK',
  safety_multiplier: 1.5,
  enabled: true,
  suppress_suggestion: false,
});
projected = project('/api/v1/reflection', reflection, [calibration]);
assert.equal(projected.calibration[0].preference.safety_multiplier, 1.5);
assert.equal(projected.calibration[0].preference.enabled, true);
assert.equal(projected.calibration[0].preference._pending, true);

console.log('reflection overlay: ok');
