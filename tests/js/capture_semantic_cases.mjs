import assert from 'node:assert/strict';

globalThis.window = { Capacitor: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = { documentElement: {}, querySelector() { return null; }, addEventListener() {}, activeElement: null };
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };

const { reconcileCaptureCandidates } = await import('../../src/student_execution_os/web/static/js/capture.js');
const { parseTask } = await import('../../src/student_execution_os/web/static/js/nlparse.js');
const { eventCreatePayload } = await import('../../src/student_execution_os/web/static/js/events.js');

const localEvent = { kind: 'EVENT', payload: { title: 'Созвон с Ариадной', starts_at: '2026-09-30T15:00:00Z',
  ends_at: '2026-09-30T15:30:00Z', remind_before_minutes: 50, category: 'MEETING' } };
const wrongTask = { kind: 'TASK', payload: { title: 'Созвон', remind_at: '2026-09-30T15:00:00Z',
  actual_cutoff: { state: 'KNOWN', at: '2026-09-30T15:00:00Z' } } };
const conflict = reconcileCaptureCandidates(localEvent, wrongTask);
assert.equal(conflict.kind, 'EVENT');
assert.equal(conflict.payload.remind_before_minutes, 50);
assert.equal(conflict.payload.actual_cutoff, undefined, 'cross-kind task deadline must not leak into event');
assert.deepEqual(conflict.conflicts.map((x) => x.field), ['kind']);

const shiftedModel = { kind: 'EVENT', payload: { title: 'Созвон с Ариадной', starts_at: '2026-09-30T16:00:00Z',
  ends_at: '2026-09-30T16:30:00Z', remind_before_minutes: 50, category: 'MEETING' } };
const timeConflict = reconcileCaptureCandidates(localEvent, shiftedModel);
assert.equal(timeConflict.payload.starts_at, localEvent.payload.starts_at);
assert.deepEqual(timeConflict.conflicts.map((x) => x.field), ['starts_at', 'ends_at']);
const equivalent = reconcileCaptureCandidates(localEvent, { kind: 'EVENT', payload: {
  starts_at: '2026-09-30T18:00:00+03:00', ends_at: '2026-09-30T18:30:00+03:00' } });
assert.deepEqual(equivalent.conflicts, [], 'equivalent instants are compatible across ISO representations');
const durationConflict = reconcileCaptureCandidates({ kind: 'EVENT', payload: { ...localEvent.payload,
  duration_minutes: 30 } }, { kind: 'EVENT', payload: { duration_minutes: 60 } });
assert.equal(durationConflict.payload.duration_minutes, 30);
assert.deepEqual(durationConflict.conflicts.map((x) => x.field), ['duration_minutes']);
const filledDeadline = reconcileCaptureCandidates({ kind: 'TASK', payload: {
  title: 'Сдать лабу', actual_cutoff: { state: 'UNKNOWN' } } }, { kind: 'TASK', payload: {
  actual_cutoff: { state: 'KNOWN', at: '2026-09-30T17:00:00Z' } } });
assert.equal(filledDeadline.payload.actual_cutoff.state, 'KNOWN', 'unknown is not an explicit deadline conflict');
assert.deepEqual(filledDeadline.conflicts, []);

const now = new Date('2026-09-29T12:00:00+03:00');
const seed = parseTask('созвон с ариадной в 18:00 завтра на пол часа.\nНапомни за 50 минут до начала', now);
assert.equal(seed.kind, 'EVENT');
assert.equal(seed.title, 'Созвон с Ариадной');
assert.equal(seed.duration_minutes, 30);
assert.equal(seed.remind_before_minutes, 50);
assert.equal(seed.starts_at, '2026-09-30T15:00:00.000Z');
assert.equal(seed.ends_at, '2026-09-30T15:30:00.000Z');

for (const [text, title] of [
  ['Встреча с Димой завтра в 18:00 на час, напомни за 15 минут', 'Встреча с Димой'],
  ['Приём у врача завтра в 10:00 на полчаса, напомни за 50 минут до начала', 'Приём у врача'],
  ['Лекция по матану завтра в 12:00 на 90 минут, напомни за 30 минут', 'Лекция по матану'],
  ['Семинар по алгебре завтра в 14:00 на час', 'Семинар по алгебре'],
]) assert.equal(parseTask(text, now).title, title, text);

assert.equal(eventCreatePayload({ title: 'Созвон', starts_at: seed.starts_at, ends_at: seed.ends_at,
  remind_before_minutes: 50 }).remind_before_minutes, 50);

console.log('capture semantic reconciliation: ok');
