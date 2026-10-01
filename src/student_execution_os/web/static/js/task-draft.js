// The task card's fields: their editors, the facts line, the questions about missing
// fields, and the task.create payload. Shared by capture and the task edit sheet.
import { now, t, fmtDateTime, fmtDuration, sameDay, fmtDay, fmtTime, code } from './i18n.js';
import { esc, icon, chipGroup, localInputValue, chipValue, isoFromLocalInput } from './ui.js';
import { durationPicker, readDuration, writeDuration, DURATION_PRESETS } from './duration.js';

export const CATEGORIES = ['HOMEWORK', 'EXAM', 'LESSON', 'WORK', 'ADMIN', 'ERRAND', 'PERSONAL_APPOINTMENT', 'MEETING', 'GENERAL'];

export const IMPORTANCE = ['LOW', 'NORMAL', 'HIGH', 'CRITICAL'];

const CHUNK_MIN_PRESETS = [15, 30, 45, 60, 90];

const CHUNK_MAX_PRESETS = [30, 45, 60, 90, 120, 180];

export const FIELDS = ['title', 'description', 'category', 'importance', 'estimated_total_effort_minutes', 'actual_cutoff',
  'target_at', 'actionable_from', 'remind_at', 'splittable', 'min_chunk_minutes', 'max_chunk_minutes', 'count_total', 'count_unit'];

export function endOfDay(offsetDays) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(23, 59, 0, 0);
  return d;
}

export function at(offsetDays, hour, minute = 0) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(hour, minute, 0, 0);
  return d;
}

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
  rows.push(['clock', t('card.effort'), draft.estimated_total_effort_minutes == null ? t('capture.provisionalEffort') : effortText(draft.estimated_total_effort_minutes), draft.estimated_total_effort_minutes == null ? 'muted' : 'accent', 'effort']);
  if (draft.importance && draft.importance !== 'NORMAL') rows.push(['alert', t('card.importance'), code('importanceShort', draft.importance), draft.importance === 'LOW' ? 'muted' : 'warn', 'importance']);
  if (draft.category && draft.category !== 'GENERAL') rows.push(['task', t('card.category'), code('category', draft.category), 'accent', 'category']);
  const when = windowText(draft);
  if (when) rows.push(['calendar', t('card.when'), when, 'accent', 'when']);
  if (draft.remind_at) rows.push(['bell', t('card.remind'), fmtDateTime(draft.remind_at), 'accent', 'remind']);
  if (draft.splittable) rows.push(['repeat', t('card.chunks'), t('card.chunks.value', { min: fmtDuration(draft.min_chunk_minutes || 30), max: fmtDuration(draft.max_chunk_minutes || 90) }), 'muted', 'chunks']);
  return rows;
}

export function factsHtml(draft) {
  return `<div class="capture-facts">${facts(draft).map(([ic, label, value, tone, target]) => `
    <button type="button" class="fact" data-fact="${esc(target)}"><span class="fact-icon tone-${esc(tone)}">${icon(ic)}</span>
      <span class="fact-copy"><small>${esc(label)}</small><strong>${esc(value)}</strong></span></button>`).join('')}</div>`;
}

export function field(label, control, hint = '', name = '') {
  return `<label class="field" ${name ? `data-field="${esc(name)}"` : ''}><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
}

export function fieldsHtml(draft, { remaining = false } = {}) {
  const cutoff = draft.actual_cutoff || { state: 'UNKNOWN' };
  return `<div class="form task-fields">
    ${field(t('form.title'), `<input data-f="title" maxlength="300" value="${esc(draft.title || '')}" placeholder="${esc(t('form.taskPlaceholder'))}">`, '', 'title')}
    <div class="field" data-field="deadline"><span>${esc(t('form.deadline'))}</span>
      ${chipGroup('f-deadline', [['KNOWN', t('form.deadline.exact')], ['ABSENT', t('form.deadline.none')], ['UNKNOWN', t('form.deadline.unknown')]], cutoff.state)}
      <input type="datetime-local" data-f="cutoff" class="${cutoff.state === 'KNOWN' ? '' : 'hidden'}" value="${esc(localInputValue(cutoff.at))}">
    </div>
    <div class="field" data-field="effort"><span>${esc(t('form.effort'))}</span>
      ${durationPicker('f-effort', draft.estimated_total_effort_minutes, { unknown: true })}</div>
    ${remaining ? `<div class="field" data-field="remaining"><span>${esc(t('form.remaining'))}</span>
      ${durationPicker('f-remaining', draft.remaining_effort_minutes)}<small class="help">${esc(t('form.remainingHelp'))}</small></div>` : ''}
    <div class="field" data-field="importance"><span>${esc(t('form.importance'))}</span>
      ${chipGroup('f-importance', IMPORTANCE.map((v) => [v, code('importance', v)]), draft.importance || 'NORMAL')}</div>
    ${field(t('form.category'), `<select data-f="category">${CATEGORIES.map((c) => `<option value="${c}" ${c === (draft.category || 'GENERAL') ? 'selected' : ''}>${esc(code('category', c))}</option>`).join('')}</select>`, '', 'category')}
    ${field(t('form.actionableFrom'), `<input type="datetime-local" data-f="actionable" value="${esc(localInputValue(draft.actionable_from))}">`, t('form.actionableHelp'), 'when')}
    ${field(t('form.target'), `<input type="datetime-local" data-f="target" value="${esc(localInputValue(draft.target_at))}">`, t('form.targetHelp'))}
    ${field(t('form.remind'), `<input type="datetime-local" data-f="remind" value="${esc(localInputValue(draft.remind_at))}">`, t('form.remindHelp'), 'remind')}
    <div class="field" data-field="chunks"><span>${esc(t('form.split'))}</span>${chipGroup('f-split', [['false', t('form.split.no')], ['true', t('form.split.yes')]], String(Boolean(draft.splittable)))}
      <div class="${draft.splittable ? '' : 'hidden'}" data-split-fields>
        <div class="field"><span>${esc(t('form.minChunk'))}</span>${durationPicker('f-min', draft.min_chunk_minutes ?? 30, { presets: CHUNK_MIN_PRESETS })}</div>
        <div class="field"><span>${esc(t('form.maxChunk'))}</span>${durationPicker('f-max', draft.max_chunk_minutes ?? 90, { presets: CHUNK_MAX_PRESETS })}</div>
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
  const effort = readDuration(root, 'f-effort');
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
    min_chunk_minutes: splittable ? readDuration(root, 'f-min') : null,
    max_chunk_minutes: splittable ? readDuration(root, 'f-max') : null,
  };
  const countTotal = Math.round(Number($f('count-total')?.value || 0));
  fields.count_total = countTotal > 0 ? countTotal : null;
  fields.count_unit = fields.count_total ? ($f('count-unit')?.value.trim() || null) : null;
  if (root.querySelector('[data-duration="f-remaining"]')) fields.remaining_effort_minutes = readDuration(root, 'f-remaining');
  return fields;
}

export function bindFields(root) {
  root.addEventListener('chipchange', (e) => {
    const $f = (name) => root.querySelector(`[data-f="${name}"]`);
    if (e.detail.name === 'f-split') root.querySelector('[data-split-fields]').classList.toggle('hidden', e.detail.value !== 'true');
    if (e.detail.name === 'f-deadline') {
      $f('cutoff').classList.toggle('hidden', e.detail.value !== 'KNOWN');
      if (e.detail.value === 'KNOWN' && !$f('cutoff').value) $f('cutoff').value = localInputValue(endOfDay(1));
    }
  });
}

export function setChip(root, name, value) {
  const group = root.querySelector(`[data-chip-group="${name}"]`);
  if (!group) return;
  group.querySelectorAll('.chip-toggle').forEach((b) => {
    const on = b.dataset.value === String(value);
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  });
}

// Pushes parsed values into the detail form without disturbing what the user is typing.
export function writeFields(root, draft) {
  const active = document.activeElement;
  const set = (name, value) => { const el = root.querySelector(`[data-f="${name}"]`); if (el && el !== active) el.value = value; };
  set('title', draft.title || '');
  set('description', draft.description || '');
  set('category', draft.category || 'GENERAL');
  const cutoff = draft.actual_cutoff || { state: 'UNKNOWN' };
  setChip(root, 'f-deadline', cutoff.state);
  set('cutoff', localInputValue(cutoff.at));
  root.querySelector('[data-f="cutoff"]')?.classList.toggle('hidden', cutoff.state !== 'KNOWN');
  writeDuration(root, 'f-effort', draft.estimated_total_effort_minutes, { unknown: true });
  setChip(root, 'f-importance', draft.importance || 'NORMAL');
  set('actionable', localInputValue(draft.actionable_from));
  set('target', localInputValue(draft.target_at));
  set('remind', localInputValue(draft.remind_at));
  setChip(root, 'f-split', String(Boolean(draft.splittable)));
  root.querySelector('[data-split-fields]')?.classList.toggle('hidden', !draft.splittable);
  writeDuration(root, 'f-min', draft.min_chunk_minutes ?? 30);
  writeDuration(root, 'f-max', draft.max_chunk_minutes ?? 90);
}

export function questionsHtml(unresolved) {
  const out = [];
  if (unresolved.includes('estimated_total_effort_minutes')) {
    out.push(`<div class="question" data-question="effort"><strong>${esc(t('q.effort'))}</strong>
      <div class="chip-row">${[...DURATION_PRESETS.map((m) => [m, fmtDuration(m)]), ['other', t('duration.other')], ['unknown', t('duration.unknown')]]
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

export function answer(kind, value) {
  if (kind === 'effort' && value === 'other') return null; // opens the hours/minutes field
  if (kind === 'effort') return { estimated_total_effort_minutes: value === 'unknown' ? null : Number(value) };
  if (value === 'none') return { actual_cutoff: { state: 'ABSENT' } };
  if (value === 'unknown') return { actual_cutoff: { state: 'UNKNOWN' } };
  if (value === 'today') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(0).toISOString() } };
  if (value === 'tomorrow') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(1).toISOString() } };
  if (value === 'week') return { actual_cutoff: { state: 'KNOWN', at: endOfDay(7).toISOString() } };
  return null; // "pick": opens the date field
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
  if (payload.estimated_total_effort_minutes == null) payload.provisional_effort = true;
  return payload;
}
