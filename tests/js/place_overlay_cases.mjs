import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-10-06T06:00:00Z') => ({ state: 'PENDING', queued_at: at,
  operation: { op_id: `op-${type}-${entity_id}-${at}`, type, entity_id, payload: payload || {} } });
const base = { places: [{ id: 'place-home', display_name: 'Дом', alias: null, has_coordinates: true, in_use: {} }],
  location_triggers: [], route_estimates: [], current_location: { state: 'UNKNOWN' }, current_place_id: null };

// A place created offline is listed at once — without exact data in the model.
const created = project('/api/v1/places', base, [op('place.create', 'place-hse', { display_name: 'Высшая школа экономики',
  alias: 'ВШЭ', address: 'Покровский бульвар 11', latitude: 55.75, longitude: 37.64 })]);
assert.equal(created.places.length, 2);
assert.equal(created.places[1].alias, 'ВШЭ');
assert.equal(created.places[1].has_coordinates, true);
assert.ok(!JSON.stringify(created).includes('Покровский'));
assert.ok(!JSON.stringify(created).includes('55.75'));

// «Я сейчас дома» with an expiry; a manual route; a trigger.
const day = project('/api/v1/places', base, [
  op('location.set', '', { state: 'KNOWN', place_id: 'place-home', expires_in_minutes: 180 }),
  op('place.create', 'place-hse', { display_name: 'ВШЭ' }, '2026-10-06T06:01:00Z'),
  op('travel.estimate.set', 'estimate-1', { origin_place_id: 'place-home', destination_place_id: 'place-hse', expected_minutes: 35, safe_minutes: 45 }, '2026-10-06T06:02:00Z'),
  op('location_trigger.create', 'trigger-1', { place_id: 'place-home', transition: 'ENTER', title: 'Разобрать вещи' }, '2026-10-06T06:03:00Z'),
]);
assert.equal(day.current_location.place, 'Дом');
assert.equal(day.current_location.expires_at, '2026-10-06T09:00:00.000Z');
assert.equal(day.route_estimates[0].safe_duration_minutes, 45);
assert.equal(day.location_triggers[0].place_name, 'Дом');
assert.equal(day.location_triggers[0].status, 'ARMED');

// Fire (device) → FIRED; delete a place → its triggers go too.
const fired = project('/api/v1/places', { ...base, location_triggers: [{ id: 'trigger-1', place_id: 'place-home', transition: 'ENTER', status: 'ARMED', repeat: false }] },
  [op('location_trigger.fire', 'trigger-1', { transition: 'ENTER', occurred_at: '2026-10-06T15:00:00Z' })]);
assert.equal(fired.location_triggers[0].status, 'FIRED');
const gone = project('/api/v1/places', { ...base, location_triggers: [{ id: 'trigger-1', place_id: 'place-home', status: 'ARMED' }] },
  [op('place.delete', 'place-home', {})]);
assert.equal(gone.places.length, 0);
assert.equal(gone.location_triggers.length, 0);
console.log('place overlay: ok');
