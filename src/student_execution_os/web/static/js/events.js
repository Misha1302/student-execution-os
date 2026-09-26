// Fixed-time events ("пара", "созвон", "с 21 до 22 провести занятие"): a start, an
// end and optionally a reminder N minutes before. Creating, editing, cancelling and
// deleting an event are queued operations (event.*), so they work offline exactly
// like task changes.
import { peek } from './store.js';
import { t, code, fmtDay, fmtTime, fmtDuration, sameDay, now } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, kv, chip } from './ui.js';
import { change, lifecycle } from './actions.js';
import { newEntityId } from './sync.js';

export const EVENT_CATEGORIES = ['LESSON', 'EXAM', 'MEETING', 'WORK', 'PERSONAL_APPOINTMENT', 'GENERAL'];
export const LEADS = [['', 'event.lead.none'], ['0', 'event.lead.0'], ['5', 'event.lead.5'], ['15', 'event.lead.15'],
  ['30', 'event.lead.30'], ['60', 'event.lead.60']];
export const DEFAULT_LEAD = 15;

const minutesBetween = (a, b) => Math.round((new Date(b) - new Date(a)) / 60000);

// "Сегодня, 21:00–22:00 · 1 ч"
export function eventWhen(e) {
  const start = new Date(e.starts_at);
  const end = new Date(e.ends_at);
  const span = sameDay(start, end) ? `${fmtTime(start)}–${fmtTime(end)}` : `${fmtTime(start)}–${fmtDay(end)}, ${fmtTime(end)}`;
  return `${fmtDay(start)}, ${span} · ${fmtDuration(minutesBetween(start, end))}`;
}

// Other active events that overlap [starts_at, ends_at), from what the device knows.
export function conflictsFor(draft, excludeId = null) {
  const known = new Map();
  for (const e of peek('/api/v1/events') || []) known.set(e.id, e);
  for (const e of peek('/api/v1/today')?.plan?.canonical_events || []) if (!known.has(e.id)) known.set(e.id, e);
  for (const e of peek('/api/v1/plan/agenda?days=7')?.plan?.canonical_events || []) if (!known.has(e.id)) known.set(e.id, e);
  const start = new Date(draft.starts_at);
  const end = new Date(draft.ends_at);
  return [...known.values()].filter((e) => e.id !== excludeId && e.status !== 'CANCELLED'
    && new Date(e.starts_at) < end && new Date(e.ends_at) > start);
}

export function conflictHtml(draft, excludeId) {
  if (!draft.starts_at || !draft.ends_at) return '';
  const list = conflictsFor(draft, excludeId);
  if (!list.length) return '';
  return `<div class="banner warn" data-event-conflict>${icon('alert')}<div><strong>${esc(t('event.overlap'))}</strong>
    <p>${list.map((e) => esc(t('event.overlapWith', { title: e.title, time: `${fmtTime(e.starts_at)}–${fmtTime(e.ends_at)}` }))).join('<br>')}</p></div></div>`;
}

function field(label, control, hint = '') {
  return `<label class="field"><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
}

export function eventFieldsHtml(draft) {
  const lead = draft.remind_before_minutes == null ? '' : String(draft.remind_before_minutes);
  return `<div class="form event-fields">
    ${field(t('form.title'), `<input data-e="title" maxlength="300" value="${esc(draft.title || '')}" placeholder="${esc(t('form.eventPlaceholder'))}">`)}
    <div class="field-row">
      ${field(t('form.starts'), `<input type="datetime-local" data-e="start" value="${esc(localInputValue(draft.starts_at))}">`)}
      ${field(t('form.ends'), `<input type="datetime-local" data-e="end" value="${esc(localInputValue(draft.ends_at))}">`)}
    </div>
    <p class="help" data-e-duration>${draft.starts_at && draft.ends_at ? esc(t('event.duration', { d: fmtDuration(minutesBetween(draft.starts_at, draft.ends_at)) })) : ''}</p>
    <div class="field"><span>${esc(t('event.remind'))}</span>${chipGroup('e-lead', LEADS.map(([v, k]) => [v, t(k)]), lead)}</div>
    <div class="field"><span>${esc(t('form.attendance'))}</span>
      ${chipGroup('e-attendance', ['REQUIRED', 'PREFERRED', 'OPTIONAL'].map((v) => [v, code('attendance', v)]), draft.attendance_policy || 'REQUIRED')}
      <small class="help">${esc(t('event.attendanceHelp'))}</small></div>
    ${field(t('form.category'), `<select data-e="category">${EVENT_CATEGORIES.map((c) => `<option value="${c}" ${c === (draft.category || 'GENERAL') ? 'selected' : ''}>${esc(code('category', c))}</option>`).join('')}</select>`)}
    ${field(t('form.description'), `<textarea data-e="description" rows="2" maxlength="5000">${esc(draft.description || '')}</textarea>`)}
  </div>`;
}

// Reads the event fields; throws a user-facing Error when they do not make an event.
export function readEventFields(root) {
  const $e = (name) => root.querySelector(`[data-e="${name}"]`);
  const startsAt = isoFromLocalInput($e('start').value);
  const endsAt = isoFromLocalInput($e('end').value);
  if (!startsAt || !endsAt) throw new Error(t('form.titleAndTime'));
  if (new Date(endsAt) <= new Date(startsAt)) throw new Error(t('event.endBeforeStart'));
  const lead = chipValue(root, 'e-lead');
  return {
    title: $e('title').value.trim(),
    description: $e('description').value.trim() || null,
    category: $e('category').value,
    starts_at: startsAt,
    ends_at: endsAt,
    attendance_policy: chipValue(root, 'e-attendance') || 'REQUIRED',
    remind_before_minutes: lead === '' || lead == null ? null : Number(lead),
  };
}

// Moving the start keeps the duration; the duration line and overlap warning follow.
export function bindEventFields(root, { excludeId = null, onChange = () => {} } = {}) {
  const $e = (name) => root.querySelector(`[data-e="${name}"]`);
  let previousStart = isoFromLocalInput($e('start').value);
  const refresh = () => {
    const s = isoFromLocalInput($e('start').value);
    const e = isoFromLocalInput($e('end').value);
    const line = root.querySelector('[data-e-duration]');
    if (line) line.textContent = s && e && new Date(e) > new Date(s) ? t('event.duration', { d: fmtDuration(minutesBetween(s, e)) }) : t('event.endBeforeStart');
    root.querySelector('[data-event-conflict]')?.remove();
    if (s && e && new Date(e) > new Date(s)) root.querySelector('.event-fields')?.insertAdjacentHTML('afterbegin', conflictHtml({ starts_at: s, ends_at: e }, excludeId));
    onChange();
  };
  $e('start').addEventListener('change', () => {
    const s = isoFromLocalInput($e('start').value);
    const e = isoFromLocalInput($e('end').value);
    if (s && previousStart && e) {
      const keep = new Date(e) - new Date(previousStart);
      $e('end').value = localInputValue(new Date(new Date(s).getTime() + Math.max(keep, 15 * 60000)));
    }
    previousStart = s;
    refresh();
  });
  $e('end').addEventListener('change', refresh);
  root.addEventListener('chipchange', () => onChange());
  refresh();
}

export function eventCreatePayload(fields) {
  const payload = {
    title: fields.title,
    starts_at: fields.starts_at,
    ends_at: fields.ends_at,
    category: fields.category || 'GENERAL',
    importance: fields.importance || 'NORMAL',
    attendance_policy: fields.attendance_policy || 'REQUIRED',
    // A journey the Assistant read ("from home to campus") is kept, not reset to none.
    location_effect: fields.location_effect?.kind ? fields.location_effect : { kind: 'NONE' },
  };
  if (fields.description) payload.description = fields.description;
  if (fields.remind_before_minutes != null) payload.remind_before_minutes = fields.remind_before_minutes;
  if (fields.arrival_requirement_minutes) payload.arrival_requirement_minutes = fields.arrival_requirement_minutes;
  if (fields.assistant_batch_id) payload.assistant_batch_id = fields.assistant_batch_id;
  return payload;
}

export async function createEvent(fields, { toastText } = {}) {
  const id = newEntityId('event');
  const result = await change('event.create', id, eventCreatePayload(fields), { success: toastText ?? t('compose.eventCreated') });
  return result ? id : null;
}

function nextHour() {
  const d = now();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return d;
}

// "+ Событие": a blank event form.
export function newEventSheet(prefill = {}) {
  const start = prefill.starts_at ? new Date(prefill.starts_at) : nextHour();
  const draft = { starts_at: start.toISOString(), ends_at: new Date(start.getTime() + 60 * 60000).toISOString(), remind_before_minutes: DEFAULT_LEAD, category: 'GENERAL', ...prefill };
  const dialog = openSheet({
    title: t('compose.event'),
    full: true,
    body: eventFieldsHtml(draft),
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('compose.create'))}</button>`,
  });
  bindEventFields(dialog);
  setTimeout(() => dialog.querySelector('[data-e="title"]').focus(), 80);
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    let fields;
    try { fields = readEventFields(dialog); } catch (err) { toast(err.message, { error: true }); return; }
    if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
    if (await createEvent(fields)) dialog.close('saved');
  });
  return dialog;
}

// What changed between an event and the edited fields (for event.update).
export function eventChanges(event, fields) {
  const out = {};
  const instant = (v) => (v ? new Date(v).getTime() : null);
  for (const key of ['title', 'description', 'category', 'attendance_policy']) {
    if ((fields[key] ?? null) !== (event[key] ?? null)) out[key] = fields[key];
  }
  if (instant(fields.starts_at) !== instant(event.starts_at)) out.starts_at = fields.starts_at;
  if (instant(fields.ends_at) !== instant(event.ends_at)) out.ends_at = fields.ends_at;
  if ((fields.remind_before_minutes ?? null) !== (event.remind_before_minutes ?? null)) out.remind_before_minutes = fields.remind_before_minutes;
  return out;
}

// Tapping an event: see it, edit it, cancel/restore or delete it.
export function eventSheet(e) {
  const active = e.status === 'ACTIVE';
  const dialog = openSheet({
    eyebrow: e.location_effect?.kind === 'MOVE' ? t('cal.bookedMove') : t('cal.fixedEvent'),
    title: e.title,
    full: active,
    body: active ? eventFieldsHtml(e) : `<p>${chip(code('status', e.status), 'muted')}</p>
      <dl class="kv-list">${kv(t('card.when'), eventWhen(e))}</dl>`,
    actions: active
      ? `<button type="button" class="button danger ghost" data-cancel>${esc(t('event.cancel'))}</button>
         <button type="button" class="button danger ghost" data-delete>${esc(t('lifecycle.delete'))}</button>
         <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`
      : `<button type="button" class="button danger ghost" data-delete>${esc(t('lifecycle.delete'))}</button>
         <button type="button" class="button" data-reopen>${esc(t('event.restore'))}</button>`,
  });
  if (active) bindEventFields(dialog, { excludeId: e.id });
  dialog.querySelector('[data-cancel]')?.addEventListener('click', () => { dialog.close(); lifecycle(e.id, e.version, 'cancel', { title: e.title, kind: 'event' }); });
  dialog.querySelector('[data-reopen]')?.addEventListener('click', () => { dialog.close(); lifecycle(e.id, e.version, 'reopen', { kind: 'event' }); });
  dialog.querySelector('[data-delete]')?.addEventListener('click', () => { dialog.close(); lifecycle(e.id, e.version, 'delete', { title: e.title, kind: 'event' }); });
  dialog.querySelector('[data-save]')?.addEventListener('click', async () => {
    let fields;
    try { fields = readEventFields(dialog); } catch (err) { toast(err.message, { error: true }); return; }
    if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
    const changes = eventChanges(e, fields);
    if (!Object.keys(changes).length) { dialog.close('unchanged'); return; }
    if (await change('event.update', e.id, changes, { success: t('event.saved') })) dialog.close('saved');
  });
  return dialog;
}
