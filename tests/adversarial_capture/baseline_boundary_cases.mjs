import assert from 'node:assert/strict';

globalThis.window = { Capacitor: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = { documentElement: {}, querySelector() { return null; }, addEventListener() {}, activeElement: null };
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };

const { mergeReminderDraft } = await import('../../src/student_execution_os/web/static/js/capture.js');
const { eventCreatePayload } = await import('../../src/student_execution_os/web/static/js/events.js');
const { parseTask, captureKind } = await import('../../src/student_execution_os/web/static/js/nlparse.js');

const manual = mergeReminderDraft(
  { title: 'Молоко', remind_at: '2026-09-30T07:00:00Z', delivery: 'PUSH', whenChosen: true, deliveryChosen: true },
  { title: 'Купить молоко', remind_at: '2026-09-30T08:00:00Z', delivery: 'ALARM' },
  { delivery: 'PUSH_AND_ALARM', wake_check: true },
);
assert.equal(manual.remind_at, '2026-09-30T07:00:00Z', 'manual reminder time must win');
assert.equal(manual.delivery, 'PUSH', 'manual delivery must win');

const payload = eventCreatePayload({
  title: 'Созвон', starts_at: '2026-09-30T15:00:00Z', ends_at: '2026-09-30T15:30:00Z',
  category: 'MEETING', attendance_policy: 'REQUIRED', remind_before_minutes: 50,
});
assert.equal(payload.remind_before_minutes, 50, 'arbitrary lead must reach payload unchanged');

const now = new Date('2026-09-29T12:00:00+03:00');
assert.equal(captureKind(parseTask('Подготовиться к встрече завтра до 18:00', now), 'Подготовиться к встрече завтра до 18:00'), 'TASK');
assert.equal(parseTask('Завтра в 18:00 созвон с Ариадной на полчаса', now).kind, 'EVENT');

console.log('baseline capture boundaries: ok');
