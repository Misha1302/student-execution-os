import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';
import { expandLocal, localRid } from '../../src/student_execution_os/web/static/js/checkin-overlay.js';

const op = (type, entity_id, payload, at = '2026-10-06T06:04:00Z', id = null) => ({
  state: 'PENDING', queued_at: at,
  operation: { op_id: id || `op-${type}-${entity_id}-${JSON.stringify(payload || {}).length}-${at}`, type, entity_id, payload: payload || {} },
});

const occ = (extra = {}) => ({
  kind: 'CHECKIN_OCCURRENCE', template_id: 'checkin-vit', original_recurrence_id: '2026-10-06T09:00:00',
  title: 'Витамин D', checkin_kind: 'MEDICATION', scheduled_at: '2026-10-06T06:00:00Z', scheduled_local: '2026-10-06T09:00:00',
  status: 'PENDING', resolved_by: null, occurred_at: null, quantity_done: 0, target_quantity: null, reminder_id: 'checkin-r1',
  version: 1, ...extra,
});
const id = { template_id: 'checkin-vit', original_recurrence_id: '2026-10-06T09:00:00' };
const now = new Date('2026-10-06T07:00:00Z');

// «Принял» offline: DONE with the pressed moment, on Today and on the check-in list.
const today = project('/api/v1/today', { plan: { blocks: [] }, checkins: [occ()] },
  [op('checkin.occurrence.done', 'checkin-vit', { ...id, occurred_at: '2026-10-06T06:04:00Z' })], { now });
assert.equal(today.checkins[0].status, 'DONE');
assert.equal(today.checkins[0].occurred_at, '2026-10-06T06:04:00Z');
assert.equal(today.checkins[0]._pending, true);

// Snoozing the prompt is a reminder operation: the outcome stays open.
const snoozed = project('/api/v1/today', { plan: { blocks: [] }, checkins: [occ()] },
  [op('reminder.snooze', 'checkin-r1', { until: '2026-10-06T06:20:00Z' })], { now });
assert.equal(snoozed.checkins[0].status, 'PENDING');

// A second outcome on a recorded day does not overwrite it (the server answers CONFLICT).
const race = project('/api/v1/today', { plan: { blocks: [] }, checkins: [occ({ status: 'DONE', resolved_by: 'USER' })] },
  [op('checkin.occurrence.skip', 'checkin-vit', id)], { now });
assert.equal(race.checkins[0].status, 'DONE');

// MISSED by policy can still be answered later (late offline «Принял»).
const late = project('/api/v1/today', { plan: { blocks: [] }, checkins: [occ({ status: 'MISSED', resolved_by: 'POLICY' })] },
  [op('checkin.occurrence.done', 'checkin-vit', { ...id, occurred_at: '2026-10-06T06:10:00Z' })], { now });
assert.equal(late.checkins[0].status, 'DONE');

// Quota progress from two devices adds up and closes the day at the target.
const quota = occ({ template_id: 'checkin-q', checkin_kind: 'QUOTA', target_quantity: 20, unit: 'задач', quantity_done: 3 });
const qid = { template_id: 'checkin-q', original_recurrence_id: '2026-10-06T09:00:00' };
const counted = project('/api/v1/today', { plan: { blocks: [] }, checkins: [quota] },
  [op('checkin.occurrence.progress', 'checkin-q', { ...qid, count: 7 }, '2026-10-06T06:00:00Z', 'a1'),
    op('checkin.occurrence.progress', 'checkin-q', { ...qid, count: 5 }, '2026-10-06T06:01:00Z', 'a2')], { now });
assert.equal(counted.checkins[0].quantity_done, 15);
assert.equal(counted.checkins[0].remaining_quantity, 5);
assert.equal(counted.checkins[0].status, 'PENDING');
const reached = project('/api/v1/today', { plan: { blocks: [] }, checkins: [quota] },
  [op('checkin.occurrence.progress', 'checkin-q', { ...qid, count: 17 })], { now });
assert.equal(reached.checkins[0].status, 'DONE');

// Moving one occurrence keeps its identity.
const moved = project('/api/v1/today', { plan: { blocks: [] }, checkins: [occ()] },
  [op('checkin.occurrence.move', 'checkin-vit', { ...id, target_local: '2026-10-06T22:00' })], { now });
assert.equal(moved.checkins[0].original_recurrence_id, '2026-10-06T09:00:00');
assert.equal(moved.checkins[0].moved_to_local, '2026-10-06T22:00:00');

// A check-in created offline shows today's occurrence before the server answers.
const localNine = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 21, 0);
const created = project('/api/v1/today', { plan: { blocks: [] }, checkins: [] }, [op('checkin.create', 'checkin-new1', {
  kind: 'ROUTINE', title: 'Полить цветы', dtstart_local: localRid(localNine).slice(0, 16), recurrence_rule: 'FREQ=DAILY',
  timezone_name: 'UTC' })], { now });
assert.equal(created.checkins.length, 1);
assert.equal(created.checkins[0].title, 'Полить цветы');
assert.equal(created.checkins[0]._pending, true);

// The list: pending template with today/upcoming; delete hides it everywhere.
const list = project('/api/v1/checkins', { checkins: [], reminder_series: [] }, [op('checkin.create', 'checkin-new1', {
  kind: 'MEDICATION', title: 'Сертралин', dtstart_local: localRid(localNine).slice(0, 16), recurrence_rule: 'FREQ=DAILY',
  timezone_name: 'UTC' }), op('reminder_series.create', 'series-1', { title: 'Мусор', dtstart_local: '2026-10-06T22:30',
  recurrence_rule: 'FREQ=DAILY', timezone_name: 'UTC' })], { now });
assert.equal(list.checkins[0].checkin_kind, 'MEDICATION');
assert.equal(list.checkins[0].today.length, 1);
assert.equal(list.checkins[0].upcoming.length >= 1, true);
assert.equal(list.reminder_series[0].title, 'Мусор');
const gone = project('/api/v1/checkins', { checkins: [{ id: 'checkin-vit', title: 'x', status: 'ACTIVE', today: [occ()] }], reminder_series: [] },
  [op('checkin.delete', 'checkin-vit', {})], { now });
assert.equal(gone.checkins.length, 0);

// History counts follow queued outcomes.
const detail = project('/api/v1/checkins/checkin-vit', { id: 'checkin-vit', title: 'Витамин D', history: [
  { local_date: '2026-10-06', done: 0, total: 1, open: 1, occurrences: [occ()] }] },
[op('checkin.occurrence.done', 'checkin-vit', id)], { now });
assert.equal(detail.history[0].done, 1);
assert.equal(detail.history[0].open, 0);

// Series occurrence skip shows on the cached reminder list.
const reminders = project('/api/v1/reminders', [{ id: 'rseries-1', kind: 'REMINDER', title: 'Мусор', status: 'SCHEDULED',
  remind_at: '2026-10-06T19:30:00Z', series: { series_id: 'series-1', original_recurrence_id: '2026-10-06T22:30:00' } }],
[op('reminder_series.occurrence.skip', 'series-1', { original_recurrence_id: '2026-10-06T22:30:00' })], { now });
assert.equal(reminders[0].status, 'CANCELLED');

// Weekday expansion skips weekends and respects the first day.
const weekdays = expandLocal({ dtstart_local: '2026-10-08T08:00', recurrence_rule: 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR' },
  new Date(2026, 9, 8), new Date(2026, 9, 15)).map(localRid);
assert.deepEqual(weekdays, ['2026-10-08T08:00:00', '2026-10-09T08:00:00', '2026-10-12T08:00:00', '2026-10-13T08:00:00', '2026-10-14T08:00:00']);

console.log('checkin overlay: ok');
