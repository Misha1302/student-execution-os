// One capture flow for typed text, dictated speech and manual details:
//
//   "+" → "Что нужно сделать?" → text or voice → task card → Создать
//
// The on-device parser (nlparse.js) fills the card instantly and offline. When the
// server has a language model configured, its validated proposal refines the card
// (fields the user already set stay as the user set them). Creating always goes
// through the offline operation queue (task.create), so a simple task is saved
// without network or model and replayed exactly once after reconnect.
import { api } from './api.js';
import { load, peek } from './store.js';
import { t, code, fmtDuration, fmtDateTime, fmtTime, fmtDay, now, getLocale, sameDay } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, setBusy, errorMessage } from './ui.js';
import { mutate, change, shell } from './actions.js';
import { newEntityId, settled } from './sync.js';
import { parseTask } from './nlparse.js';
import { startDictation, voiceSupported } from './native.js';
import { reachWarning } from './health.js';
import { parseCommand } from './commands.js';
import { renderCommands, knownItems, isCommand } from './command-preview.js';
import { createReminder, deliveryChips, hasAlarm } from './reminders.js';
import { eventFieldsHtml, readEventFields, bindEventFields, eventWhen, conflictHtml, createEvent, DEFAULT_LEAD, LEADS } from './events.js';

export const CATEGORIES = ['HOMEWORK', 'EXAM', 'LESSON', 'WORK', 'ADMIN', 'ERRAND', 'PERSONAL_APPOINTMENT', 'MEETING', 'GENERAL'];
export const IMPORTANCE = ['LOW', 'NORMAL', 'HIGH', 'CRITICAL'];
const EFFORT_CHOICES = [15, 30, 60, 120, 180];
const FIELDS = ['title', 'description', 'category', 'importance', 'estimated_total_effort_minutes', 'actual_cutoff',
  'target_at', 'actionable_from', 'remind_at', 'splittable', 'min_chunk_minutes', 'max_chunk_minutes', 'count_total', 'count_unit'];

export const deviceTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

function endOfDay(offsetDays) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(23, 59, 0, 0);
  return d;
}

function at(offsetDays, hour, minute = 0) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(hour, minute, 0, 0);
  return d;
}

// ---- human wording for task values -------------------------------------------------

export function deadlineText(cutoff) {
  if (!cutoff || cutoff.state === 'UNKNOWN') return t('card.deadline.unknown');
  if (cutoff.state === 'ABSENT') return t('card.deadline.none');
  return fmtDateTime(cutoff.at);
}

export function effortText(minutes) {
  return minutes == null ? t('card.effort.unknown') : t('card.effort.value', { d: fmtDuration(minutes) });
}

function windowText(task) {
  const from = task.actionable_from ? new Date(task.actionable_from) : null;
  const to = task.target_at ? new Date(task.target_at) : null;
  if (from && to && sameDay(from, to)) return `${fmtDay(from)}, ${fmtTime(from)}–${fmtTime(to)}`;
  if (from && to) return t('card.when.range', { from: fmtDateTime(from), to: fmtDateTime(to) });
  if (from) return t('card.when.from', { when: fmtDateTime(from) });
  if (to) return t('card.when.by', { when: fmtDateTime(to) });
  return '';
}

function facts(draft) {
  const rows = [];
  const cutoff = draft.actual_cutoff || { state: 'UNKNOWN' };
  rows.push(['flag', t('card.deadline'), deadlineText(cutoff), cutoff.state === 'KNOWN' ? 'danger' : 'muted', 'deadline']);
  rows.push(['clock', t('card.effort'), effortText(draft.estimated_total_effort_minutes), draft.estimated_total_effort_minutes == null ? 'muted' : 'accent', 'effort']);
  if (draft.importance && draft.importance !== 'NORMAL') rows.push(['alert', t('card.importance'), code('importanceShort', draft.importance), draft.importance === 'LOW' ? 'muted' : 'warn', 'importance']);
  if (draft.category && draft.category !== 'GENERAL') rows.push(['task', t('card.category'), code('category', draft.category), 'accent', 'category']);
  const when = windowText(draft);
  if (when) rows.push(['calendar', t('card.when'), when, 'accent', 'when']);
  if (draft.remind_at) rows.push(['bell', t('card.remind'), fmtDateTime(draft.remind_at), 'accent', 'remind']);
  if (draft.splittable) rows.push(['repeat', t('card.chunks'), t('card.chunks.value', { min: fmtDuration(draft.min_chunk_minutes || 30), max: fmtDuration(draft.max_chunk_minutes || 90) }), 'muted', 'chunks']);
  return rows;
}

function factsHtml(draft) {
  return `<div class="capture-facts">${facts(draft).map(([ic, label, value, tone, target]) => `
    <button type="button" class="fact" data-fact="${esc(target)}"><span class="fact-icon tone-${esc(tone)}">${icon(ic)}</span>
      <span class="fact-copy"><small>${esc(label)}</small><strong>${esc(value)}</strong></span></button>`).join('')}</div>`;
}

// ---- the editable field set (capture "more details" and the task edit sheet) -------

function field(label, control, hint = '', name = '') {
  return `<label class="field" ${name ? `data-field="${esc(name)}"` : ''}><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
}

export function fieldsHtml(draft, { remaining = false } = {}) {
  const cutoff = draft.actual_cutoff || { state: 'UNKNOWN' };
  const effort = draft.estimated_total_effort_minutes;
  const effortChoice = effort == null ? 'unknown' : EFFORT_CHOICES.includes(Number(effort)) ? String(effort) : 'custom';
  return `<div class="form task-fields">
    ${field(t('form.title'), `<input data-f="title" maxlength="300" value="${esc(draft.title || '')}" placeholder="${esc(t('form.taskPlaceholder'))}">`, '', 'title')}
    <div class="field" data-field="deadline"><span>${esc(t('form.deadline'))}</span>
      ${chipGroup('f-deadline', [['KNOWN', t('form.deadline.exact')], ['ABSENT', t('form.deadline.none')], ['UNKNOWN', t('form.deadline.unknown')]], cutoff.state)}
      <input type="datetime-local" data-f="cutoff" class="${cutoff.state === 'KNOWN' ? '' : 'hidden'}" value="${esc(localInputValue(cutoff.at))}">
    </div>
    <div class="field" data-field="effort"><span>${esc(t('form.effort'))}</span>
      ${chipGroup('f-effort', [...EFFORT_CHOICES.map((m) => [String(m), fmtDuration(m)]), ['custom', t('form.custom')], ['unknown', t('form.effortUnknown')]], effortChoice)}
      <input type="number" inputmode="numeric" min="1" step="5" data-f="effort" class="${effortChoice === 'custom' ? '' : 'hidden'}" value="${esc(effort ?? '')}" placeholder="${esc(t('form.minutes'))}">
    </div>
    ${remaining ? field(t('form.remaining'), `<input type="number" inputmode="numeric" min="0" step="5" data-f="remaining" value="${esc(draft.remaining_effort_minutes ?? '')}">`, t('form.remainingHelp')) : ''}
    <div class="field" data-field="importance"><span>${esc(t('form.importance'))}</span>
      ${chipGroup('f-importance', IMPORTANCE.map((v) => [v, code('importance', v)]), draft.importance || 'NORMAL')}</div>
    ${field(t('form.category'), `<select data-f="category">${CATEGORIES.map((c) => `<option value="${c}" ${c === (draft.category || 'GENERAL') ? 'selected' : ''}>${esc(code('category', c))}</option>`).join('')}</select>`, '', 'category')}
    ${field(t('form.actionableFrom'), `<input type="datetime-local" data-f="actionable" value="${esc(localInputValue(draft.actionable_from))}">`, t('form.actionableHelp'), 'when')}
    ${field(t('form.target'), `<input type="datetime-local" data-f="target" value="${esc(localInputValue(draft.target_at))}">`, t('form.targetHelp'))}
    ${field(t('form.remind'), `<input type="datetime-local" data-f="remind" value="${esc(localInputValue(draft.remind_at))}">`, t('form.remindHelp'), 'remind')}
    <div class="field" data-field="chunks"><span>${esc(t('form.split'))}</span>${chipGroup('f-split', [['false', t('form.split.no')], ['true', t('form.split.yes')]], String(Boolean(draft.splittable)))}
      <div class="field-row ${draft.splittable ? '' : 'hidden'}" data-split-fields>
        ${field(t('form.minChunk'), `<input type="number" inputmode="numeric" min="5" step="5" data-f="min" value="${esc(draft.min_chunk_minutes ?? 30)}">`)}
        ${field(t('form.maxChunk'), `<input type="number" inputmode="numeric" min="5" step="5" data-f="max" value="${esc(draft.max_chunk_minutes ?? 90)}">`)}
      </div></div>
    <div class="field" data-field="count"><span>${esc(t('form.count'))}</span>
      <div class="field-row">
        <input type="number" inputmode="numeric" min="1" max="100000" step="1" data-f="count-total" value="${esc(draft.count_total ?? draft.count_progress?.total ?? '')}" placeholder="${esc(t('form.countTotal'))}">
        <input data-f="count-unit" maxlength="40" value="${esc(draft.count_unit ?? draft.count_progress?.unit ?? '')}" placeholder="${esc(t('form.countUnit'))}">
      </div><small class="help">${esc(t('form.countHelp'))}</small></div>
    ${field(t('form.description'), `<textarea data-f="description" rows="3" maxlength="5000">${esc(draft.description || '')}</textarea>`, '', 'description')}
  </div>`;
}

// Reads the field set; returns [fields, changedName] or throws a user-facing error.
export function readFields(root) {
  const $f = (name) => root.querySelector(`[data-f="${name}"]`);
  const deadline = chipValue(root, 'f-deadline');
  let cutoff = { state: deadline || 'UNKNOWN' };
  if (deadline === 'KNOWN') {
    const value = isoFromLocalInput($f('cutoff').value);
    if (!value) throw new Error(t('form.deadlineRequired'));
    cutoff = { state: 'KNOWN', at: value };
  }
  const effortChoice = chipValue(root, 'f-effort');
  let effort = null;
  if (effortChoice === 'custom') {
    effort = Math.round(Number($f('effort').value));
    if (!effort || effort <= 0) throw new Error(t('form.effortRequired'));
  } else if (effortChoice && effortChoice !== 'unknown') effort = Number(effortChoice);
  const splittable = chipValue(root, 'f-split') === 'true';
  const fields = {
    title: $f('title').value.trim(),
    description: $f('description').value.trim() || null,
    category: $f('category').value,
    importance: chipValue(root, 'f-importance') || 'NORMAL',
    estimated_total_effort_minutes: effort,
    actual_cutoff: cutoff,
    actionable_from: isoFromLocalInput($f('actionable').value),
    target_at: isoFromLocalInput($f('target').value),
    remind_at: isoFromLocalInput($f('remind').value),
    splittable,
    min_chunk_minutes: splittable ? Number($f('min').value) || null : null,
    max_chunk_minutes: splittable ? Number($f('max').value) || null : null,
  };
  const countTotal = Math.round(Number($f('count-total')?.value || 0));
  fields.count_total = countTotal > 0 ? countTotal : null;
  fields.count_unit = fields.count_total ? ($f('count-unit')?.value.trim() || null) : null;
  if ($f('remaining')) fields.remaining_effort_minutes = $f('remaining').value === '' ? null : Math.max(0, Math.round(Number($f('remaining').value)));
  return fields;
}

export function bindFields(root) {
  root.addEventListener('chipchange', (e) => {
    const $f = (name) => root.querySelector(`[data-f="${name}"]`);
    if (e.detail.name === 'f-effort') $f('effort').classList.toggle('hidden', e.detail.value !== 'custom');
    if (e.detail.name === 'f-split') root.querySelector('[data-split-fields]').classList.toggle('hidden', e.detail.value !== 'true');
    if (e.detail.name === 'f-deadline') {
      $f('cutoff').classList.toggle('hidden', e.detail.value !== 'KNOWN');
      if (e.detail.value === 'KNOWN' && !$f('cutoff').value) $f('cutoff').value = localInputValue(endOfDay(1));
    }
  });
}

function setChip(root, name, value) {
  const group = root.querySelector(`[data-chip-group="${name}"]`);
  if (!group) return;
  group.querySelectorAll('.chip-toggle').forEach((b) => {
    const on = b.dataset.value === String(value);
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  });
}

// Pushes parsed values into the detail form without disturbing what the user is typing.
function writeFields(root, draft) {
  const active = document.activeElement;
  const set = (name, value) => { const el = root.querySelector(`[data-f="${name}"]`); if (el && el !== active) el.value = value; };
  set('title', draft.title || '');
  set('description', draft.description || '');
  set('category', draft.category || 'GENERAL');
  const cutoff = draft.actual_cutoff || { state: 'UNKNOWN' };
  setChip(root, 'f-deadline', cutoff.state);
  set('cutoff', localInputValue(cutoff.at));
  root.querySelector('[data-f="cutoff"]')?.classList.toggle('hidden', cutoff.state !== 'KNOWN');
  const effort = draft.estimated_total_effort_minutes;
  const choice = effort == null ? 'unknown' : EFFORT_CHOICES.includes(Number(effort)) ? String(effort) : 'custom';
  setChip(root, 'f-effort', choice);
  set('effort', effort ?? '');
  root.querySelector('[data-f="effort"]')?.classList.toggle('hidden', choice !== 'custom');
  setChip(root, 'f-importance', draft.importance || 'NORMAL');
  set('actionable', localInputValue(draft.actionable_from));
  set('target', localInputValue(draft.target_at));
  set('remind', localInputValue(draft.remind_at));
  setChip(root, 'f-split', String(Boolean(draft.splittable)));
  root.querySelector('[data-split-fields]')?.classList.toggle('hidden', !draft.splittable);
  set('min', draft.min_chunk_minutes ?? 30);
  set('max', draft.max_chunk_minutes ?? 90);
}

// ---- questions for what the parser could not determine ------------------------------

function questionsHtml(unresolved) {
  const out = [];
  if (unresolved.includes('estimated_total_effort_minutes')) {
    out.push(`<div class="question" data-question="effort"><strong>${esc(t('q.effort'))}</strong>
      <div class="chip-row">${[[30, fmtDuration(30)], [60, t('q.effort.1h')], [120, t('q.effort.2h')], ['unknown', t('q.dontKnow')]]
        .map(([v, label]) => `<button type="button" class="chip-toggle" data-answer="effort" data-value="${esc(v)}">${esc(label)}</button>`).join('')}</div>
      <small class="help">${esc(t('q.effortHelp'))}</small></div>`);
  }
  if (unresolved.includes('actual_cutoff')) {
    out.push(`<div class="question" data-question="deadline"><strong>${esc(t('q.deadline'))}</strong>
      <div class="chip-row">${[['today', t('day.today')], ['tomorrow', t('day.tomorrow')], ['week', t('form.deadline.week')], ['pick', t('form.deadline.exact')], ['none', t('form.deadline.none')], ['unknown', t('q.dontKnow')]]
        .map(([v, label]) => `<button type="button" class="chip-toggle" data-answer="deadline" data-value="${esc(v)}">${esc(label)}</button>`).join('')}</div></div>`);
  }
  return out.join('');
}

function answer(kind, value) {
  if (kind === 'effort') return { estimated_total_effort_minutes: value === 'unknown' ? null : Number(value) };
  if (value === 'none') return { actual_cutoff: { state: 'ABSENT' } };
  if (value === 'unknown') return { actual_cutoff: { state: 'UNKNOWN' } };
  if (value === 'today') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(0).toISOString() } };
  if (value === 'tomorrow') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(1).toISOString() } };
  if (value === 'week') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(7).toISOString() } };
  return null; // "pick": opens the date field
}

// ---- capabilities (cached; offline → local only) ------------------------------------

async function capabilities() {
  const cached = peek('/api/v1/ask/capabilities');
  if (cached) return cached;
  try {
    return (await load('/api/v1/ask/capabilities')).data;
  } catch { return null; }
}

// Which interpreter read the text is always on screen: a language model, or the
// on-device parser — and if the model was meant to help but did not, why.
export function engineLine(state, { model = null, reason = null } = {}) {
  if (state === 'thinking') return { tone: 'muted', icon: 'spark', text: t('capture.engine.thinking') };
  if (state === 'ai') return { tone: 'accent', icon: 'spark', text: model ? t('capture.engine.aiModel', { model }) : t('capture.engine.ai') };
  if (state === 'fallback') {
    const key = `capture.engine.why.${reason}`;
    const why = t(key) === key ? t('capture.engine.why.other') : t(key);
    return { tone: 'warn', icon: 'alert', text: t('capture.engine.fallback', { why }) };
  }
  if (state === 'offline') return { tone: 'muted', icon: 'task', text: t('capture.engine.offline') };
  if (state === 'noai') return { tone: 'muted', icon: 'task', text: t('capture.engine.noAi') };
  return { tone: 'muted', icon: 'task', text: t('capture.engine.local') };
}

// Payload for task.create from a card draft; unanswered questions mean "don't know".
export function createPayload(draft) {
  const payload = {};
  for (const key of FIELDS) if (key in draft) payload[key] = draft[key];
  payload.title = String(draft.title || '').trim();
  if (!payload.actual_cutoff) payload.actual_cutoff = { state: 'UNKNOWN' };
  if (payload.remind_at && new Date(payload.remind_at) <= now()) payload.remind_at = null;
  for (const key of ['target_at', 'actionable_from', 'remind_at', 'description']) if (payload[key] == null) delete payload[key];
  if (!payload.splittable) { delete payload.min_chunk_minutes; delete payload.max_chunk_minutes; }
  if (!payload.count_total) { delete payload.count_total; delete payload.count_unit; }
  return payload;
}

// A fixed-time event read as a task (the user said "это задача"): the slot becomes
// the work window and its length the effort; nothing is due.
function taskFromEvent(e) {
  return {
    title: e.title,
    description: e.description ?? null,
    category: e.category || 'GENERAL',
    importance: e.importance || 'NORMAL',
    actionable_from: e.starts_at,
    target_at: e.ends_at,
    estimated_total_effort_minutes: Math.max(5, Math.round((new Date(e.ends_at) - new Date(e.starts_at)) / 60000)),
    actual_cutoff: { state: 'ABSENT' },
  };
}

function eventFromTask(d) {
  const start = d.actionable_from ? new Date(d.actionable_from) : (() => { const x = now(); x.setMinutes(0, 0, 0); x.setHours(x.getHours() + 1); return x; })();
  const minutes = Number(d.estimated_total_effort_minutes) || 60;
  return {
    title: d.title, description: d.description ?? null, category: d.category || 'GENERAL', importance: d.importance || 'NORMAL',
    starts_at: start.toISOString(), ends_at: new Date(start.getTime() + minutes * 60000).toISOString(),
    attendance_policy: 'REQUIRED', remind_before_minutes: DEFAULT_LEAD,
  };
}

// Presence-aware reminder enrichment. `undefined` means the provider omitted a
// field, not "reset it"; explicit card edits outrank deterministic intent, which
// in turn is a floor under AI enrichment.
export function mergeReminderDraft(previous, incoming, floor = null) {
  const old = previous || {};
  const next = { ...old };
  for (const key of ['title', 'remind_at', 'note', 'delivery', 'wake_check', 'raise_volume', 'obligation_id']) {
    if (key in (incoming || {})) next[key] = incoming[key];
  }
  next.deliveryChosen = Boolean(old.deliveryChosen);
  next.wakeChosen = Boolean(old.wakeChosen);
  if (old.deliveryChosen) next.delivery = old.delivery;
  if (old.wakeChosen) next.wake_check = old.wake_check;
  if (!next.delivery) next.delivery = 'PUSH';
  if (!('wake_check' in next)) next.wake_check = false;
  if (!('raise_volume' in next)) next.raise_volume = hasAlarm(next.delivery);
  if (floor && !next.deliveryChosen) {
    if (floor.delivery === 'PUSH_AND_ALARM' || next.delivery === 'PUSH') next.delivery = floor.delivery;
  }
  if (floor?.wake_check && !next.wakeChosen) next.wake_check = true;
  return next;
}

// ---- the sheet ----------------------------------------------------------------------

export function openCapture({ text = '', listen: listenNow = false } = {}) {
  let draft = { title: '', importance: 'NORMAL', category: 'GENERAL', estimated_total_effort_minutes: null, actual_cutoff: { state: 'UNKNOWN' }, splittable: false };
  let unresolved = [];
  const answered = new Set();
  let assistant = null; // { batch_id, action_id } when the model's proposal is on the card
  let commands = null;  // a proposal about existing items ("готово …"), local or from the server
  let serverSeq = 0;
  let serverTimer = null;
  let parseTimer = null;
  let kind = 'TASK';           // what the card will create: 'TASK' | 'EVENT' | 'REMINDER'
  let kindChosen = false;      // the user picked the kind; parses no longer switch it
  let eventDraft = null;
  let reminderDraft = null;   // { title, remind_at, delivery, wake_check, raise_volume, note }
  let reminderFloor = null;   // explicit alarm/wake semantics from the local parser
  const eventEdited = new Set(); // event fields the user set by hand
  let dictation = null;

  const dialog = openSheet({
    title: t('capture.title'),
    full: true,
    body: `<div class="capture">
      <div class="capture-input">
        <textarea id="capture-text" rows="2" maxlength="4000" enterkeyhint="done" placeholder="${esc(t('capture.placeholder'))}">${esc(text)}</textarea>
        ${voiceSupported() ? `<button type="button" class="icon-button mic" data-mic aria-pressed="false" aria-label="${esc(t('capture.voice'))}">${icon('mic')}</button>` : ''}
      </div>
      <div class="voice-panel" data-voice hidden>
        <span class="voice-dot" aria-hidden="true"></span>
        <span class="voice-copy"><strong data-voice-state></strong><small data-voice-text></small></span>
        <button type="button" class="button small" data-voice-stop>${esc(t('voice.stop'))}</button>
      </div>
      <p class="help" data-capture-hint>${esc(t('capture.hint'))}</p>
      <div data-capture-status class="capture-status" hidden></div>
      <div data-preview class="capture-preview" hidden></div>
      <p class="engine-line" data-engine hidden></p>
      <div data-commands hidden></div>
      <details class="details" data-more>
        <summary>${icon('settings')} ${esc(t('capture.more'))}</summary>
        <div data-task-details>${fieldsHtml(draft)}</div>
        <div data-event-details hidden></div>
      </details>
      <div class="capture-other">
        <button type="button" class="link" data-other="event">${icon('event')} ${esc(t('capture.event'))}</button>
        <button type="button" class="link" data-other="recurring">${icon('repeat')} ${esc(t('capture.recurring'))}</button>
      </div>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-create disabled>${esc(t('compose.create'))}</button>`,
  });
  const input = dialog.querySelector('#capture-text');
  const preview = dialog.querySelector('[data-preview]');
  const details = dialog.querySelector('[data-more]');
  const createButton = dialog.querySelector('[data-create]');
  const status = dialog.querySelector('[data-capture-status]');
  const taskDetails = details.querySelector('[data-task-details]');
  const eventDetails = details.querySelector('[data-event-details]');
  bindFields(taskDetails);

  const showStatus = (message) => { status.hidden = !message; status.textContent = message || ''; };
  const engineEl = dialog.querySelector('[data-engine]');
  const showEngine = (state, extra) => {
    const line = engineLine(state, extra);
    engineEl.hidden = !String(input.value).trim();
    engineEl.dataset.engine = state;
    engineEl.className = `engine-line tone-${line.tone}`;
    engineEl.innerHTML = `${icon(line.icon)}<span>${esc(line.text)}</span>`;
  };

  function merge(fields, source) {
    for (const key of FIELDS) {
      if (answered.has(key) || !(key in fields)) continue;
      draft[key] = fields[key];
    }
    if (source === 'local' && !String(input.value).trim()) draft.title = '';
  }

  // A parse (local or the model's) that found a fixed-time event.
  function adoptEvent(parsed) {
    const next = { ...(eventDraft || { attendance_policy: 'REQUIRED', remind_before_minutes: DEFAULT_LEAD }) };
    for (const key of ['title', 'description', 'category', 'importance', 'starts_at', 'ends_at',
      'attendance_policy', 'location_effect', 'arrival_requirement_minutes', 'remind_before_minutes']) {
      if (!eventEdited.has(key) && parsed[key] !== undefined) next[key] = parsed[key];
    }
    eventDraft = next;
    if (!kindChosen) kind = 'EVENT';
    merge(taskFromEvent(eventDraft), 'event');
  }

  function ensureEventDetails() {
    if (eventDetails.dataset.ready) return;
    eventDetails.dataset.ready = '1';
    eventDetails.innerHTML = eventFieldsHtml(eventDraft);
    bindEventFields(eventDetails, { onChange: fromEventDetails });
    eventDetails.querySelector('[data-e="title"]').addEventListener('input', fromEventDetails);
  }

  function writeEventFields() {
    if (!eventDetails.dataset.ready) return;
    const active = document.activeElement;
    const set = (name, value) => { const el = eventDetails.querySelector(`[data-e="${name}"]`); if (el && el !== active) el.value = value; };
    set('title', eventDraft.title || '');
    set('start', localInputValue(eventDraft.starts_at));
    set('end', localInputValue(eventDraft.ends_at));
    set('category', eventDraft.category || 'GENERAL');
    setChip(eventDetails, 'e-lead', eventDraft.remind_before_minutes == null ? '' : String(eventDraft.remind_before_minutes));
  }

  function fromEventDetails() {
    let fields;
    try { fields = readEventFields(eventDetails); } catch { return; }
    for (const [key, value] of Object.entries(fields)) {
      if (JSON.stringify(value ?? null) !== JSON.stringify(eventDraft?.[key] ?? null)) eventEdited.add(key);
    }
    eventDraft = { ...eventDraft, ...fields };
    render();
  }

  function eventCardHtml() {
    const lead = eventDraft.remind_before_minutes == null ? '' : String(eventDraft.remind_before_minutes);
    return `<article class="capture-card event-card" data-kind="EVENT">
      <span class="eyebrow">${icon('event')} ${esc(t('capture.kindEvent'))}</span>
      <h3 class="capture-title">${esc(eventDraft.title || '')}</h3>
      <div class="capture-facts">
        <button type="button" class="fact" data-fact="event-time"><span class="fact-icon tone-accent">${icon('calendar')}</span>
          <span class="fact-copy"><small>${esc(t('card.when'))}</small><strong data-event-when>${esc(eventWhen(eventDraft))}</strong></span></button>
        <div class="fact static"><span class="fact-icon tone-muted">${icon('flag')}</span>
          <span class="fact-copy"><small>${esc(t('card.deadline'))}</small><strong>${esc(t('event.noDeadline'))}</strong></span></div>
      </div>
      <div class="field"><span>${icon('bell')} ${esc(t('event.remind'))}</span>${chipGroup('card-lead', LEADS.map(([v, k]) => [v, t(k)]), lead)}</div>
      ${conflictHtml(eventDraft)}
      ${eventDraft.remind_before_minutes != null ? reachWarning() : ''}
      <button type="button" class="link" data-switch-kind="TASK">${esc(t('capture.asTask'))}</button>
    </article>`;
  }

  function reminderCardHtml() {
    const r = reminderDraft;
    return `<article class="capture-card reminder-card" data-kind="REMINDER">
      <span class="eyebrow">${icon('bell')} ${esc(t(hasAlarm(r.delivery) ? 'reminder.kindAlarm' : 'reminder.kind'))}</span>
      <h3 class="capture-title">${esc(r.title || '')}</h3>
      <div class="capture-facts">
        <div class="fact static"><span class="fact-icon tone-accent">${icon('clock')}</span>
          <span class="fact-copy"><small>${esc(t('reminder.when'))}</small><strong>${esc(r.remind_at ? fmtDateTime(r.remind_at) : '—')}</strong></span></div>
      </div>
      <div class="field"><span>${esc(t('reminder.how'))}</span>${deliveryChips('card-delivery', r.delivery)}</div>
      ${hasAlarm(r.delivery) ? `<div class="field"><span>${esc(t('reminder.wake'))}</span>${chipGroup('card-wake', [['false', t('reminder.wake.no')], ['true', t('reminder.wake.yes')]], String(Boolean(r.wake_check)))}</div>` : ''}
      ${reachWarning({ alarm: hasAlarm(r.delivery) })}
      <button type="button" class="link" data-switch-kind="TASK">${esc(t('capture.reminderAsTask'))}</button>
    </article>`;
  }

  function render() {
    // A command about existing items replaces the creation form entirely.
    const commandMode = Boolean(commands);
    createButton.hidden = commandMode;
    details.hidden = commandMode;
    dialog.querySelector('.capture-other').hidden = commandMode;
    if (kind === 'REMINDER' && reminderDraft) {
      const hasTitle = Boolean(String(reminderDraft.title || '').trim());
      createButton.disabled = !hasTitle || !reminderDraft.remind_at || Boolean(commands);
      preview.hidden = !hasTitle || Boolean(commands);
      dialog.querySelector('[data-capture-hint]').hidden = hasTitle || Boolean(commands);
      taskDetails.hidden = true;
      eventDetails.hidden = true;
      if (hasTitle && !commands) preview.innerHTML = reminderCardHtml();
      return;
    }
    const isEvent = kind === 'EVENT' && eventDraft;
    const hasTitle = Boolean(String((isEvent ? eventDraft.title : draft.title) || '').trim());
    createButton.disabled = !hasTitle || Boolean(commands);
    preview.hidden = !hasTitle || Boolean(commands);
    dialog.querySelector('[data-capture-hint]').hidden = hasTitle || Boolean(commands);
    taskDetails.hidden = Boolean(isEvent);
    eventDetails.hidden = !isEvent;
    if (isEvent) {
      ensureEventDetails();
      writeEventFields();
      if (hasTitle && !commands) preview.innerHTML = eventCardHtml();
      return;
    }
    if (hasTitle && !commands) {
      const open = unresolved.filter((f) => !answered.has(f));
      preview.innerHTML = `<article class="capture-card">
        <h3 class="capture-title">${esc(draft.title)}</h3>
        ${factsHtml(draft)}
        ${draft.description ? `<p class="muted">${esc(draft.description)}</p>` : ''}
        ${questionsHtml(open)}
        ${draft.remind_at ? reachWarning() : ''}
        <button type="button" class="link" data-switch-kind="EVENT">${esc(t('capture.asEvent'))}</button>
        ${draft.remind_at ? `<button type="button" class="link" data-switch-kind="REMINDER">${esc(t('capture.asReminder'))}</button>` : ''}
      </article>`;
    }
    writeFields(taskDetails, draft);
  }

  function adoptReminder(parsed, source = 'assistant') {
    if (source === 'local') {
      reminderFloor = hasAlarm(parsed.delivery) ? { delivery: parsed.delivery, wake_check: parsed.wake_check === true } : null;
    }
    reminderDraft = mergeReminderDraft(reminderDraft, parsed, reminderFloor);
    if (!kindChosen) kind = 'REMINDER';
    // The same moment as a task's reminder, if the user says "это задача".
    merge({ title: parsed.title, remind_at: parsed.remind_at, actual_cutoff: { state: 'ABSENT' } }, 'reminder');
  }

  function showLocalCommand(action) {
    commands = { source: 'local', actions: [action] };
    renderCommands(dialog.querySelector('[data-commands]'), commands, { onDone: () => dialog.close('applied') });
    showEngine('local');
    render();
  }

  function parseLocal() {
    const raw = input.value;
    commands = null;
    dialog.querySelector('[data-commands]').hidden = true;
    assistant = null;
    // "готово эссе", "перенеси созвон на 19:00": about something the user already has.
    const command = parseCommand(raw, now(), knownItems());
    if (command) { showLocalCommand(command); return; }
    const parsed = parseTask(raw, now());
    reminderFloor = null;
    unresolved = parsed.unresolved || [];
    if (parsed.kind === 'EVENT') {
      unresolved = [];
      adoptEvent(parsed);
    } else if (parsed.kind === 'REMINDER') {
      unresolved = [];
      adoptReminder(parsed, 'local');
    } else {
      if (!kindChosen) kind = 'TASK';
      merge(parsed, 'local');
    }
    showEngine('local');
    render();
  }

  async function enrich() {
    const raw = input.value.trim();
    const command = Boolean(parseCommand(raw, now(), knownItems()));
    if (!raw) return;
    const caps = await capabilities();
    if (!command && !caps?.live_llm_provider) {
      // No model for this account (or no server answer): the device's parse stands.
      if (caps && caps.credential_status && caps.credential_status !== 'OK' && caps.credential_status !== 'UNTESTED') showEngine('fallback', { reason: caps.credential_status });
      else showEngine(caps ? 'noai' : 'offline');
      return;
    }
    const seq = ++serverSeq;
    showEngine('thinking');
    let result;
    try {
      result = await api('/api/v1/assistant/interpret', { method: 'POST', body: { text: raw, context: { timezone: deviceTimeZone(), locale: getLocale() } } });
    } catch (err) {
      if (seq !== serverSeq) return;
      showEngine(err.code === 'NETWORK' ? 'offline' : 'fallback', { reason: err.code === 'NETWORK' ? null : 'SERVER' });
      if (command && err.code !== 'NETWORK') showStatus(errorMessage(err));
      return; // the local card stays; creating still works offline
    }
    if (seq !== serverSeq || input.value.trim() !== raw) return;
    showStatus('');
    if (result.engine === 'AI') showEngine('ai', { model: result.model });
    else if (result.fallback) showEngine('fallback', { reason: result.fallback_reason });
    else showEngine(caps?.live_llm_provider ? 'local' : 'noai');
    const actions = result.actions || [];
    if (actions.some(isCommand)) {
      commands = { source: 'server', batchId: result.batch_id, actions };
      renderCommands(dialog.querySelector('[data-commands]'), commands, { onDone: () => dialog.close('applied') });
      render();
      return;
    }
    const reminder = actions.length === 1 && actions[0].command === 'CREATE_REMINDER' ? actions[0] : null;
    if (reminder) {
      assistant = { batch_id: result.batch_id, action_id: reminder.id };
      unresolved = [];
      adoptReminder(reminder.payload);
      render();
      return;
    }
    // The words asked for an alarm: a task or event reading of them is a downgrade
    // (an older server may still send one), so the local alarm card stays.
    if (reminderFloor && !kindChosen) return;
    const create = actions.length === 1 && actions[0].command === 'CREATE_TASK' ? actions[0] : null;
    const event = actions.length === 1 && actions[0].command === 'CREATE_EVENT' && actions[0].payload?.starts_at && actions[0].payload?.ends_at ? actions[0] : null;
    if (event) {
      // The model's reading goes through the same card and the same event.create
      // validation as the local parse; it only improves title and times.
      assistant = { batch_id: result.batch_id, action_id: event.id };
      unresolved = [];
      adoptEvent(event.payload);
      render();
    } else if (create) {
      if (!kindChosen) kind = 'TASK';
      assistant = { batch_id: result.batch_id, action_id: create.id };
      unresolved = create.unresolved_fields || [];
      merge(create.payload, 'assistant');
      render();
    }
  }

  input.addEventListener('input', () => {
    clearTimeout(parseTimer);
    clearTimeout(serverTimer);
    serverSeq += 1;
    parseTimer = setTimeout(parseLocal, 120);
    serverTimer = setTimeout(enrich, 1100);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!createButton.disabled) createButton.click();
    }
  });

  preview.addEventListener('chipchange', (e) => {
    if (e.detail.name === 'card-delivery' && reminderDraft) {
      reminderDraft = { ...reminderDraft, delivery: e.detail.value, deliveryChosen: true };
      render();
      return;
    }
    if (e.detail.name === 'card-wake' && reminderDraft) {
      reminderDraft = { ...reminderDraft, wake_check: e.detail.value === 'true', wakeChosen: true };
      return;
    }
    if (e.detail.name !== 'card-lead' || !eventDraft) return;
    eventDraft.remind_before_minutes = e.detail.value === '' ? null : Number(e.detail.value);
    eventEdited.add('remind_before_minutes');
    writeEventFields();
  });

  preview.addEventListener('click', (e) => {
    const switcher = e.target.closest('[data-switch-kind]');
    if (switcher) {
      kindChosen = true;
      kind = switcher.dataset.switchKind;
      if (kind === 'EVENT' && !eventDraft) eventDraft = eventFromTask(draft);
      if (kind === 'REMINDER' && !reminderDraft) {
        reminderDraft = { title: draft.title, remind_at: draft.remind_at, delivery: 'PUSH', wake_check: false, raise_volume: true };
      }
      if (kind === 'TASK' && eventDraft) { merge(taskFromEvent(eventDraft), 'event'); unresolved = draft.estimated_total_effort_minutes == null ? ['estimated_total_effort_minutes'] : []; }
      render();
      return;
    }
    const chip = e.target.closest('[data-answer]');
    if (chip) {
      const values = answer(chip.dataset.answer, chip.dataset.value);
      if (!values) { details.open = true; setChip(details, 'f-deadline', 'KNOWN'); details.querySelector('[data-f="cutoff"]').classList.remove('hidden'); details.querySelector('[data-f="cutoff"]').focus(); return; }
      Object.assign(draft, values);
      Object.keys(values).forEach((k) => answered.add(k));
      render();
      return;
    }
    const fact = e.target.closest('[data-fact]');
    if (fact) {
      details.open = true;
      const target = fact.dataset.fact === 'event-time' ? eventDetails.querySelector('[data-e="start"]')?.closest('.field')
        : details.querySelector(`[data-field="${fact.dataset.fact}"]`) || details.querySelector('[data-field="title"]');
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      target?.querySelector('input,select,textarea,button')?.focus({ preventScroll: true });
    }
  });

  // Any manual edit wins over later parses of the text.
  const fromDetails = () => {
    let fields;
    try { fields = readFields(taskDetails); } catch { return; }
    for (const key of FIELDS) {
      if (JSON.stringify(fields[key] ?? null) !== JSON.stringify(draft[key] ?? null)) answered.add(key);
    }
    Object.assign(draft, fields);
    render();
  };
  taskDetails.addEventListener('change', fromDetails);
  taskDetails.addEventListener('chipchange', () => setTimeout(fromDetails));
  taskDetails.querySelector('[data-f="title"]').addEventListener('input', fromDetails);

  const mic = dialog.querySelector('[data-mic]');
  const voicePanel = dialog.querySelector('[data-voice]');
  mic?.addEventListener('click', () => (dictation ? dictation.stop() : listen()));
  dialog.querySelector('[data-voice-stop]').addEventListener('click', () => dictation?.stop());
  dialog.addEventListener('close', () => { dictation?.stop(); dictation = null; });

  function voiceState(state, text = '') {
    voicePanel.hidden = state === 'idle';
    voicePanel.dataset.state = state;
    voicePanel.querySelector('[data-voice-state]').textContent = state === 'idle' ? '' : t(`voice.${state}`);
    voicePanel.querySelector('[data-voice-text]').textContent = text;
    voicePanel.querySelector('[data-voice-stop]').hidden = state !== 'recording';
    if (mic) {
      mic.classList.toggle('recording', state === 'recording');
      mic.setAttribute('aria-pressed', String(state === 'recording'));
      mic.setAttribute('aria-label', t(state === 'recording' ? 'voice.stop' : 'capture.voice'));
    }
  }

  // Dictation: tap to start, tap again (or "Стоп") to finish. The transcript goes
  // into the text field and through the same parse as typing; nothing is created
  // until the user presses "Создать".
  async function listen() {
    const before = input.value.trim();
    voiceState('starting');
    dictation = startDictation({
      onState: (state) => voiceState(state),
      onPartial: (text) => voiceState('recording', text),
    });
    try {
      const heard = await dictation.result;
      dictation = null;
      if (!heard) { voiceState('error', t('ask.voiceEmpty')); setTimeout(() => { if (!dictation) voiceState('idle'); }, 2500); return; }
      voiceState('processing', heard);
      input.value = before ? `${before} ${heard}` : heard;
      parseLocal();
      voiceState('idle');
      enrich();
    } catch (error) {
      dictation = null;
      const key = { VOICE_UNAVAILABLE: 'ask.voiceUnavailable', VOICE_DENIED: 'ask.voiceDenied' }[error.code] || 'ask.voiceFailed';
      voiceState('error', t(key));
    }
  }

  dialog.querySelectorAll('[data-other]').forEach((button) => button.addEventListener('click', async () => {
    dialog.close('other');
    const { composers } = await import('./compose.js');
    composers[button.dataset.other]();
  }));

  createButton.addEventListener('click', async (e) => {
    if (kind === 'REMINDER' && reminderDraft) {
      const fields = { ...reminderDraft, title: String(reminderDraft.title || '').trim() };
      if (assistant) fields.assistant_batch_id = assistant.batch_id;
      if (await createReminder(fields)) dialog.close('saved');
      return;
    }
    if (kind === 'EVENT' && eventDraft) {
      if (details.open) fromEventDetails();
      const fields = { ...eventDraft, title: String(eventDraft.title || '').trim() };
      if (assistant) fields.assistant_batch_id = assistant.batch_id;
      if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
      if (!(new Date(fields.ends_at) > new Date(fields.starts_at))) { toast(t('event.endBeforeStart'), { error: true }); return; }
      const id = await createEvent(fields, { toastText: t('capture.eventSaved', { when: eventWhen(fields) }) });
      if (id) dialog.close('saved');
      return;
    }
    if (details.open) fromDetails();
    const payload = createPayload(draft);
    if (!payload.title) { toast(t('form.titleRequired'), { error: true }); return; }
    if (assistant) { payload.assistant_batch_id = assistant.batch_id; }
    const taskId = newEntityId('task');
    const created = await change('task.create', taskId, payload);
    if (!created) return;
    dialog.close('saved');
    const state = await settled(created.op_id);
    toast(state === 'PENDING' ? t('capture.savedOffline') : t('compose.taskCreated'), {
      action: { label: t('common.open'), run: () => shell.go('task', { params: [taskId] }) },
    });
  });

  if (text) { parseLocal(); enrich(); }
  setTimeout(() => input.focus(), 80);
  if (listenNow && voiceSupported()) listen();
  return dialog;
}

// ---- edit and reschedule an existing task --------------------------------------------

function changedFields(task, fields) {
  const changes = {};
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const instant = (v) => (v ? new Date(v).getTime() : null);
  for (const key of ['title', 'description', 'category', 'importance', 'estimated_total_effort_minutes', 'splittable']) {
    if (!same(fields[key], task[key])) changes[key] = fields[key];
  }
  if (fields.splittable) {
    for (const key of ['min_chunk_minutes', 'max_chunk_minutes']) if (!same(fields[key], task[key])) changes[key] = fields[key];
  }
  for (const key of ['actionable_from', 'target_at', 'remind_at']) {
    if (instant(fields[key]) !== instant(task[key])) changes[key] = fields[key];
  }
  const a = fields.actual_cutoff; const b = task.actual_cutoff || {};
  if (a.state !== b.state || (a.state === 'KNOWN' && instant(a.at) !== instant(b.at))) changes.actual_cutoff = a;
  const count = task.count_progress || null;
  if ((fields.count_total ?? null) !== (count?.total ?? null)) { changes.count_total = fields.count_total; changes.count_unit = fields.count_unit; }
  else if (fields.count_total && (fields.count_unit ?? null) !== (count?.unit ?? null)) changes.count_unit = fields.count_unit;
  if ('remaining_effort_minutes' in fields && fields.remaining_effort_minutes !== task.remaining_effort_minutes
    && fields.remaining_effort_minutes != null) changes.remaining_effort_minutes = fields.remaining_effort_minutes;
  return changes;
}

export function editTaskSheet(task, { focus } = {}) {
  const started = task.remaining_effort_minutes != null && task.remaining_effort_minutes !== task.estimated_total_effort_minutes;
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('task.edit'),
    full: true,
    body: fieldsHtml(task, { remaining: started }),
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  bindFields(dialog);
  if (focus) setTimeout(() => dialog.querySelector(`[data-field="${focus}"]`)?.querySelector('input,select,button')?.focus(), 80);
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    let fields;
    try { fields = readFields(dialog); } catch (err) { toast(err.message, { error: true }); return; }
    if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
    if (fields.remind_at && new Date(fields.remind_at) <= now()) { toast(t('form.remindPast'), { error: true }); return; }
    const changes = changedFields(task, fields);
    if (!Object.keys(changes).length) { dialog.close('unchanged'); return; }
    const saved = await change('task.update', task.id, changes, { success: t('task.saved') });
    if (saved) dialog.close('saved');
  });
}

function shiftDays(iso, days) {
  const d = new Date(iso);
  d.setDate(d.getDate() + days);
  while (d <= now()) d.setDate(d.getDate() + 1);
  return d.toISOString();
}

// "Перенести": move the deadline or put the task off — each tap is one offline-safe operation.
export function rescheduleSheet(task) {
  const cutoff = task.actual_cutoff || { state: 'UNKNOWN' };
  const known = cutoff.state === 'KNOWN';
  const deadlineOptions = known
    ? [['d1', t('resched.plusDay')], ['d3', t('resched.plus3')], ['d7', t('resched.plusWeek')], ['pick', t('form.deadline.exact')], ['none', t('resched.noDeadline')]]
    : [['today', t('resched.tonight')], ['tomorrow', t('day.tomorrow')], ['week', t('form.deadline.week')], ['pick', t('form.deadline.exact')]];
  const later = [['h1', t('resched.inHour')], ...(now().getHours() < 18 ? [['evening', t('resched.evening')]] : []),
    ['morning', t('resched.tomorrowMorning')], ['pick', t('form.deadline.exact')]];
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('resched.title'),
    body: `<section class="field"><span>${esc(t('resched.deadline'))}</span>
        <p class="muted">${esc(t('resched.current', { when: deadlineText(cutoff) }))}</p>
        <div class="chip-row">${deadlineOptions.map(([v, label]) => `<button type="button" class="chip-toggle" data-deadline="${v}">${esc(label)}</button>`).join('')}</div>
        <div class="field-row hidden" data-pick-deadline><input type="datetime-local" data-f="deadline" value="${esc(localInputValue(known ? cutoff.at : endOfDay(1)))}">
          <button type="button" class="button primary" data-save-deadline>${esc(t('common.save'))}</button></div>
      </section>
      ${task.status === 'ACTIVE' || task.status === 'DRAFT' ? `<section class="field"><span>${esc(t('resched.later'))}</span>
        <p class="help">${esc(t('resched.laterHelp'))}</p>
        <div class="chip-row">${later.map(([v, label]) => `<button type="button" class="chip-toggle" data-later="${v}">${esc(label)}</button>`).join('')}</div>
        <div class="field-row hidden" data-pick-later><input type="datetime-local" data-f="later" value="${esc(localInputValue(at(1, 9)))}">
          <button type="button" class="button primary" data-save-later>${esc(t('common.save'))}</button></div>
      </section>` : ''}`,
  });
  const update = async (changes, success) => {
    const saved = await change('task.update', task.id, changes, { success });
    if (saved) dialog.close('saved');
  };
  const defer = async (until) => {
    if (new Date(until) <= now()) { toast(t('resched.past'), { error: true }); return; }
    const saved = await change('task.defer', task.id, { until }, { success: t('resched.deferred', { when: fmtDateTime(until) }) });
    if (saved) dialog.close('saved');
  };
  dialog.addEventListener('click', (e) => {
    const d = e.target.closest('[data-deadline]');
    if (d) {
      const v = d.dataset.deadline;
      if (v === 'pick') { dialog.querySelector('[data-pick-deadline]').classList.remove('hidden'); return; }
      let at_;
      if (v === 'none') { update({ actual_cutoff: { state: 'ABSENT' } }, t('resched.moved')); return; }
      if (v.startsWith('d')) at_ = shiftDays(cutoff.at, Number(v.slice(1)));
      else at_ = (v === 'today' ? endOfDay(0) : v === 'tomorrow' ? endOfDay(1) : endOfDay(7)).toISOString();
      update({ actual_cutoff: { state: 'KNOWN', at: at_ } }, t('resched.movedTo', { when: fmtDateTime(at_) }));
      return;
    }
    const l = e.target.closest('[data-later]');
    if (l) {
      const v = l.dataset.later;
      if (v === 'pick') { dialog.querySelector('[data-pick-later]').classList.remove('hidden'); return; }
      const until = v === 'h1' ? new Date(now().getTime() + 3600000) : v === 'evening' ? at(0, 19) : at(1, 9);
      defer(new Date(Math.ceil(until.getTime() / 60000) * 60000).toISOString());
    }
  });
  dialog.querySelector('[data-save-deadline]')?.addEventListener('click', () => {
    const value = isoFromLocalInput(dialog.querySelector('[data-f="deadline"]').value);
    if (!value) { toast(t('form.deadlineRequired'), { error: true }); return; }
    update({ actual_cutoff: { state: 'KNOWN', at: value } }, t('resched.movedTo', { when: fmtDateTime(value) }));
  });
  dialog.querySelector('[data-save-later]')?.addEventListener('click', () => {
    const value = isoFromLocalInput(dialog.querySelector('[data-f="later"]').value);
    if (value) defer(value);
  });
  return dialog;
}
