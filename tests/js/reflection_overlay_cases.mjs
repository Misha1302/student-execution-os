import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-09-30T16:00:00Z') => ({
  state: 'PENDING',
  queued_at: at,
  operation: { op_id: 'op-' + type + '-' + entity_id, type, entity_id, payload },
});

const task = {
  id: 'task-reflection-1',
  title: 'Architecture review',
  status: 'ACTIVE',
  remaining_effort_minutes: 90,
};

const base = {
  now: '2026-09-30T16:00:00Z',
  timezone_name: 'UTC',
  local_date: '2026-09-30',
  week_starts_on: '2026-09-28',
  intent: null,
  daily_reflection: null,
  weekly_review: null,
  focus_candidates: [task],
  calibration: {
    sample_count: 3,
    median_actual_to_planned_ratio: 1.5,
    suggested_multiplier: 1.5,
    bias: 'UNDER_ESTIMATING',
    accepted: null,
    automatic_mutation: false,
  },
};

const intent = op('intent.upsert', 'intent-2026-09-30', {
  local_date: '2026-09-30',
  timezone_name: 'UTC',
  focus_note: 'Finish the architecture pass',
  task_ids: [task.id],
  expected_version: 0,
});
let reflected = project('/api/v1/reflection', base, [intent]);
assert.equal(reflected.intent.focus_note, 'Finish the architecture pass');
assert.deepEqual(reflected.intent.task_ids, [task.id]);
assert.equal(reflected.intent.tasks[0].title, task.title);
assert.equal(reflected.intent.version, 1);
assert.equal(reflected.intent._pending, true);

const daily = op('reflection.upsert', 'reflection-2026-09-30', {
  local_date: '2026-09-30',
  timezone_name: 'UTC',
  summary: 'Good work',
  wins: 'Focus',
  blockers: 'Interruptions',
  adjustment: 'Protect time',
  expected_version: 0,
});
reflected = project('/api/v1/reflection', base, [intent, daily]);
assert.equal(reflected.daily_reflection.summary, 'Good work');
assert.equal(reflected.daily_reflection.version, 1);

const weekly = op('weekly_review.upsert', 'week-2026-09-28', {
  week_starts_on: '2026-09-28',
  timezone_name: 'UTC',
  summary: 'Good week',
  expected_version: 0,
});
reflected = project('/api/v1/reflection', base, [weekly]);
assert.equal(reflected.weekly_review.summary, 'Good week');

const accepted = op('calibration.accept', 'calibration-hint', {
  multiplier: 1.5,
  based_on_samples: 3,
  expected_version: 0,
});
reflected = project('/api/v1/reflection', base, [accepted]);
assert.equal(reflected.calibration.accepted.multiplier, 1.5);
assert.equal(reflected.calibration.accepted._pending, true);

const todayBase = {
  now: '2026-09-30T16:00:00Z',
  local_date: '2026-09-30',
  daily_intent: null,
  active_execution: null,
  tasks: [task],
  needs_refinement: [],
  next_actions: [],
  plan: { blocks: [], canonical_events: [], constraints: [] },
};
const today = project('/api/v1/today', todayBase, [intent]);
assert.equal(today.daily_intent.focus_note, 'Finish the architecture pass');
assert.equal(today.daily_intent.tasks[0].title, task.title);

console.log('reflection overlay: ok');
