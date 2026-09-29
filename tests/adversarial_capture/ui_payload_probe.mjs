globalThis.window = { Capacitor: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = { documentElement: {}, querySelector() { return null; }, addEventListener() {} };
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };

const { LEADS, DEFAULT_LEAD, eventCreatePayload } = await import('../../src/student_execution_os/web/static/js/events.js');
const leadValues = LEADS.map(([value]) => value === '' ? null : Number(value));
const sample = eventCreatePayload({
  title: 'Созвон с Ариадной', starts_at: '2026-09-30T15:00:00.000Z', ends_at: '2026-09-30T15:30:00.000Z',
  category: 'MEETING', importance: 'NORMAL', attendance_policy: 'REQUIRED', remind_before_minutes: 50,
});
process.stdout.write(JSON.stringify({ lead_values: leadValues, default_lead: DEFAULT_LEAD,
  ui_can_select_50: leadValues.includes(50), serialized_50: sample.remind_before_minutes, sample }));
