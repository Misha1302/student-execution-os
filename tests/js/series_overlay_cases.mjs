// Queued series.* operations on the cached calendar (run with TZ=UTC).
import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, n = 1) => ({
  state: 'PENDING', queued_at: `2026-09-28T09:0${n}:00Z`,
  operation: { op_id: `op-${type}-${n}`, type, entity_id, payload },
});
const occ = (rid, extra = {}) => ({ template_id: 'algebra', original_recurrence_id: rid, starts_at: `${rid}Z`.replace(':00Z', ':00.000Z'),
  ends_at: new Date(new Date(`${rid}Z`).getTime() + 90 * 60000).toISOString(), cancelled: false, changed_by: [],
  location_text: 'A-101', teacher: null, title: 'Algebra', ...extra });
const base = {
  events: [],
  horizon_end: '2026-10-28T00:00:00Z',
  recurring_templates: [{ id: 'algebra', title: 'Algebra', dtstart_local: '2026-09-21T12:00:00', duration_minutes: 90,
    recurrence_rule: 'FREQ=WEEKLY;COUNT=10', location_text: 'A-101', teacher: null, version: 1 }],
  occurrences: [occ('2026-09-28T12:00:00'), occ('2026-10-05T12:00:00'), occ('2026-10-12T12:00:00', { cancelled: true, cancelled_by: 'SOURCE', cancel_reason: 'SOURCE', changed_by: ['SOURCE'] })],
};
const find = (data, rid) => data.occurrences.find((o) => o.original_recurrence_id === rid);
const P = (items) => project('/api/v1/calendar', base, items);

// Move, then room change: both show, merged, pending.
let data = P([
  op('series.occurrence.move', 'algebra', { template_id: 'algebra', original_recurrence_id: '2026-09-28T12:00:00', starts_local: '2026-09-28T15:00:00' }, 1),
  op('series.occurrence.update', 'algebra', { template_id: 'algebra', original_recurrence_id: '2026-09-28T12:00:00', location_text: 'B-310' }, 2),
]);
let o = find(data, '2026-09-28T12:00:00');
assert.equal(o.starts_at, '2026-09-28T15:00:00.000Z');
assert.equal(o.ends_at, '2026-09-28T16:30:00.000Z');
assert.equal(o.location_text, 'B-310');
assert.deepEqual(o.changed_by, ['USER']);
assert.equal(o._pending, true);

// Cancel then restore: back to the series. Restore never revives a class the source cancelled.
data = P([
  op('series.occurrence.cancel', 'algebra', { template_id: 'algebra', original_recurrence_id: '2026-10-05T12:00:00' }, 1),
  op('series.occurrence.restore', 'algebra', { template_id: 'algebra', original_recurrence_id: '2026-10-05T12:00:00' }, 2),
  op('series.occurrence.restore', 'algebra', { template_id: 'algebra', original_recurrence_id: '2026-10-12T12:00:00' }, 3),
]);
assert.equal(find(data, '2026-10-05T12:00:00').cancelled, false);
assert.equal(find(data, '2026-10-12T12:00:00').cancelled, true);
assert.equal(find(data, '2026-10-12T12:00:00').cancelled_by, 'SOURCE');

// Holiday over a range and its undo.
data = P([op('series.holiday', 'holiday-1', { from_date: '2026-09-28', to_date: '2026-10-05' })]);
assert.deepEqual([find(data, '2026-09-28T12:00:00').cancel_reason, find(data, '2026-10-05T12:00:00').cancel_reason], ['HOLIDAY', 'HOLIDAY']);
data = P([op('series.holiday', 'holiday-1', { from_date: '2026-09-28', to_date: '2026-10-05' }, 1),
  op('series.holiday.restore', 'holiday-1', { from_date: '2026-09-28', to_date: '2026-10-05' }, 2)]);
assert.equal(find(data, '2026-09-28T12:00:00').cancelled, false);

// A new series and an extra class appear before the server has them.
data = P([
  op('series.create', 'physics', { title: 'Physics', dtstart_local: '2026-09-29T09:00:00', duration_minutes: 90, recurrence_rule: 'FREQ=WEEKLY;COUNT=3', timezone_name: 'UTC' }, 1),
  op('series.extra.create', 'extra-1', { template_id: 'algebra', starts_at: '2026-09-30T09:00:00Z' }, 2),
]);
assert.equal(data.occurrences.filter((x) => x.template_id === 'physics').length, 3);
const extra = data.events.find((e) => e.id === 'extra-1');
assert.equal(extra.ends_at, '2026-09-30T10:30:00.000Z');
assert.equal(extra.location_text, 'A-101');

// Split from a date: the old series stops there, the new one starts.
data = P([op('series.split', 'algebra-v2', { template_id: 'algebra', original_recurrence_id: '2026-10-05T12:00:00', starts_local: '2026-10-06T14:00:00', recurrence_rule: 'FREQ=WEEKLY;COUNT=2' })]);
assert.equal(data.occurrences.some((x) => x.template_id === 'algebra' && x.original_recurrence_id >= '2026-10-05'), false);
assert.deepEqual(data.occurrences.filter((x) => x.template_id === 'algebra-v2').map((x) => x.original_recurrence_id),
  ['2026-10-06T14:00:00', '2026-10-13T14:00:00']);

console.log('series overlay: ok');
