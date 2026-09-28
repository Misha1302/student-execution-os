import { load } from '../store.js';
import { t, code, fmtTime, fmtDay, fmtDuration, fmtDateTime, dayKey, now } from '../i18n.js';
import { esc, icon, chip, kv, empty, openSheet, localInputValue, isoFromLocalInput, setBusy, toast } from '../ui.js';
import { change } from '../actions.js';
import { newEntityId } from '../sync.js';
import { durationPicker, takeDuration } from '../duration.js';
import { eventSheet } from '../events.js';
import { composers } from '../compose.js';

// "FREQ=WEEKLY;INTERVAL=2;COUNT=16" → "раз в две недели, 16 раз".
function ruleText(rule) {
  const parts = Object.fromEntries(String(rule || '').split(';').map((p) => p.split('=')));
  const every = parts.FREQ === 'DAILY' ? t('form.repeat.daily') : parts.INTERVAL === '2' ? t('form.repeat.biweekly') : t('form.repeat.weekly');
  return parts.COUNT ? t('cal.ruleCount', { every, n: parts.COUNT }) : every;
}

// "2026-09-28T12:00" from an instant, in the device zone (the calendar's local time).
const localValue = (iso) => localInputValue(iso).slice(0, 16);
const withSeconds = (local) => (local.length === 16 ? `${local}:00` : local);

function occurrenceWhy(o) {
  if (o.cancelled) return t(o.cancelled_by === 'SOURCE' ? 'series.cancelledBySource' : o.cancel_reason === 'HOLIDAY' ? 'series.cancelledHoliday' : 'series.cancelledByYou');
  if ((o.changed_by || []).includes('USER')) return t('series.changedByYou');
  if ((o.changed_by || []).includes('SOURCE')) return t('series.changedBySource');
  return '';
}

function occurrenceSheet(o, template) {
  const ident = { template_id: o.template_id, original_recurrence_id: o.original_recurrence_id };
  const mine = (o.changed_by || []).includes('USER');
  const imported = Boolean(template?.imported);
  const why = occurrenceWhy(o);
  const dialog = openSheet({
    eyebrow: t('cal.occurrence'),
    title: o.title || template?.title || '',
    body: `${why ? `<p>${chip(why, o.cancelled ? 'danger' : 'warn')}${imported ? ` ${chip(t('series.imported'), 'muted')}` : ''}</p>` : imported ? `<p>${chip(t('series.imported'), 'muted')}</p>` : ''}
      <dl class="kv-list">
        ${kv(t('plan.starts'), fmtDateTime(o.starts_at))}
        ${kv(t('plan.ends'), fmtDateTime(o.ends_at))}
        ${o.location_text ? kv(t('series.location'), o.location_text) : ''}
        ${o.teacher ? kv(t('series.teacher'), o.teacher) : ''}
        ${o.note ? kv(t('series.note'), o.note) : ''}
        ${template ? kv(t('cal.rule'), ruleText(template.recurrence_rule)) : ''}
      </dl>
      <div class="form" data-occ-edit hidden>
        <label class="field"><span>${esc(t('cal.moveTo'))}</span><input type="datetime-local" data-occ-start value="${esc(localValue(o.starts_at))}"></label>
        <label class="field"><span>${esc(t('series.location'))}</span><input data-occ-location maxlength="200" value="${esc(o.location_text || '')}"></label>
        <label class="field"><span>${esc(t('series.teacher'))}</span><input data-occ-teacher maxlength="200" value="${esc(o.teacher || '')}"></label>
        <label class="field"><span>${esc(t('series.note'))}</span><input data-occ-note maxlength="500" value="${esc(o.note || '')}"></label>
        <button type="button" class="button primary" data-occ-save>${esc(t('common.save'))}</button>
      </div>
      <p class="help">${esc(t(imported ? 'series.importedHelp' : 'cal.occurrenceHelp'))}</p>`,
    actions: `<div class="detail-actions">
      ${!o.cancelled ? `<button type="button" class="button" data-occ-open-edit>${esc(t('series.editOne'))}</button>` : ''}
      ${!o.cancelled ? `<button type="button" class="button danger ghost" data-occ-cancel>${esc(t('cal.skip'))}</button>` : ''}
      ${(mine || (o.cancelled && o.cancelled_by === 'USER')) ? `<button type="button" class="button" data-occ-restore>${esc(t('series.restore'))}</button>` : ''}
      ${!imported && !o.cancelled ? `<button type="button" class="button ghost" data-occ-split>${esc(t('series.fromHere'))}</button>` : ''}
    </div>`,
  });
  const $ = (sel) => dialog.querySelector(sel);
  $('[data-occ-open-edit]')?.addEventListener('click', () => { $('[data-occ-edit]').hidden = false; $('[data-occ-start]').focus(); });
  $('[data-occ-cancel]')?.addEventListener('click', async () => {
    if (await change('series.occurrence.cancel', o.template_id, ident, { success: t('cal.skipped'),
      undo: () => change('series.occurrence.restore', o.template_id, ident) })) dialog.close('saved');
  });
  $('[data-occ-restore]')?.addEventListener('click', async () => {
    if (await change('series.occurrence.restore', o.template_id, ident, { success: t('series.restored') })) dialog.close('saved');
  });
  $('[data-occ-split]')?.addEventListener('click', () => { dialog.close('other'); splitSheet(o, template); });
  $('[data-occ-save]')?.addEventListener('click', async (e) => {
    setBusy(e.currentTarget, true);
    const start = $('[data-occ-start]').value;
    if (start && start !== localValue(o.starts_at)) {
      await change('series.occurrence.move', o.template_id, { ...ident, starts_local: withSeconds(start) });
    }
    const details = {};
    for (const [key, sel] of [['location_text', '[data-occ-location]'], ['teacher', '[data-occ-teacher]'], ['note', '[data-occ-note]']]) {
      const value = $(sel).value.trim();
      if (value !== (o[key] || '')) details[key] = value;
    }
    if (Object.keys(details).length) await change('series.occurrence.update', o.template_id, { ...ident, ...details });
    toast(t('series.saved'));
    dialog.close('saved');
  });
}

// "From this class on": a new series (time, room, teacher, length); earlier classes stay.
function splitSheet(o, template) {
  const dialog = openSheet({
    eyebrow: template?.title || '',
    title: t('series.fromHere'),
    body: `<div class="form">
      <label class="field"><span>${esc(t('series.newStart'))}</span><input type="datetime-local" data-split-start value="${esc(localValue(o.starts_at))}"></label>
      <div class="field"><span>${esc(t('form.duration'))}</span>${durationPicker('split-duration', template?.duration_minutes || 90)}</div>
      <label class="field"><span>${esc(t('series.location'))}</span><input data-split-location maxlength="200" value="${esc(template?.location_text || '')}"></label>
      <label class="field"><span>${esc(t('series.teacher'))}</span><input data-split-teacher maxlength="200" value="${esc(template?.teacher || '')}"></label>
      <p class="help">${esc(t('series.fromHereHelp'))}</p></div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-split-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-split-save]').addEventListener('click', async () => {
    const duration = takeDuration(dialog, 'split-duration');
    if (!duration) return;
    const payload = { template_id: o.template_id, original_recurrence_id: o.original_recurrence_id,
      starts_local: withSeconds(dialog.querySelector('[data-split-start]').value), duration_minutes: duration,
      location_text: dialog.querySelector('[data-split-location]').value.trim(),
      teacher: dialog.querySelector('[data-split-teacher]').value.trim() };
    // A COUNT-limited series needs the remaining count for its successor.
    const parts = Object.fromEntries(String(template?.recurrence_rule || '').split(';').map((x) => x.split('=')));
    if (parts.COUNT) {
      const step = (parts.FREQ === 'DAILY' ? 1 : 7) * Number(parts.INTERVAL || 1) * 86400000;
      const done = Math.round((new Date(o.original_recurrence_id) - new Date(template.dtstart_local)) / step);
      payload.recurrence_rule = String(template.recurrence_rule).replace(/COUNT=\d+/, `COUNT=${Math.max(1, Number(parts.COUNT) - done)}`);
    }
    if (await change('series.split', newEntityId('series'), payload, { success: t('series.saved') })) dialog.close('saved');
  });
}

function seriesSheet(template) {
  const dialog = openSheet({
    eyebrow: t('cal.series'),
    title: template.title,
    body: `<dl class="kv-list">
        ${kv(t('cal.rule'), ruleText(template.recurrence_rule))}
        ${template.location_text ? kv(t('series.location'), template.location_text) : ''}
        ${template.teacher ? kv(t('series.teacher'), template.teacher) : ''}
      </dl>
      ${template.imported ? `<p class="help">${esc(t('series.importedHelp'))}</p>` : ''}
      <div class="form" data-extra hidden>
        <label class="field"><span>${esc(t('series.extraWhen'))}</span><input type="datetime-local" data-extra-start></label>
        <label class="field"><span>${esc(t('series.location'))}</span><input data-extra-location maxlength="200" value="${esc(template.location_text || '')}"></label>
        <button type="button" class="button primary" data-extra-save>${esc(t('series.addExtra'))}</button>
      </div>`,
    actions: `<div class="detail-actions">
      <button type="button" class="button" data-extra-open>${esc(t('series.addExtra'))}</button>
      <button type="button" class="button" data-holiday-open>${esc(t('series.holiday'))}</button></div>`,
  });
  dialog.querySelector('[data-extra-open]').addEventListener('click', () => {
    dialog.querySelector('[data-extra]').hidden = false;
    dialog.querySelector('[data-extra-start]').focus();
  });
  dialog.querySelector('[data-extra-save]').addEventListener('click', async () => {
    const at = isoFromLocalInput(dialog.querySelector('[data-extra-start]').value);
    if (!at) { toast(t('form.titleAndTime'), { error: true }); return; }
    const payload = { template_id: template.id, starts_at: at };
    const location = dialog.querySelector('[data-extra-location]').value.trim();
    if (location) payload.location_text = location;
    if (await change('series.extra.create', newEntityId('event'), payload, { success: t('series.extraAdded') })) dialog.close('saved');
  });
  dialog.querySelector('[data-holiday-open]').addEventListener('click', () => { dialog.close('other'); holidaySheet([template.id]); });
}

// Days off: every class in the range is cancelled; the same range undoes it.
function holidaySheet(templateIds = null) {
  const today = localValue(now().toISOString()).slice(0, 10);
  const dialog = openSheet({
    title: t('series.holiday'),
    body: `<div class="form">
      <label class="field"><span>${esc(t('series.holidayFrom'))}</span><input type="date" data-h-from value="${esc(today)}"></label>
      <label class="field"><span>${esc(t('series.holidayTo'))}</span><input type="date" data-h-to value="${esc(today)}"></label>
      <p class="help">${esc(t(templateIds ? 'series.holidayOneHelp' : 'series.holidayHelp'))}</p></div>`,
    actions: `<button type="button" class="button ghost" data-h-undo>${esc(t('series.holidayUndo'))}</button>
      <button type="button" class="button primary" data-h-save>${esc(t('series.holidayApply'))}</button>`,
  });
  const range = () => {
    const payload = { from_date: dialog.querySelector('[data-h-from]').value, to_date: dialog.querySelector('[data-h-to]').value };
    if (templateIds) payload.template_ids = templateIds;
    return payload;
  };
  dialog.querySelector('[data-h-save]').addEventListener('click', async () => {
    const payload = range();
    if (!payload.from_date || !payload.to_date || payload.to_date < payload.from_date) { toast(t('series.holidayRange'), { error: true }); return; }
    if (await change('series.holiday', newEntityId('holiday'), payload, { success: t('series.holidayDone'),
      undo: () => change('series.holiday.restore', newEntityId('holiday'), payload) })) dialog.close('saved');
  });
  dialog.querySelector('[data-h-undo]').addEventListener('click', async () => {
    const payload = range();
    if (await change('series.holiday.restore', newEntityId('holiday'), payload, { success: t('series.holidayUndone') })) dialog.close('saved');
  });
}

function rowNote(it) {
  if (it.kind === 'event') return [code('attendance', it.ref.attendance_policy), it.ref.location_text].filter(Boolean).join(' · ');
  return [occurrenceWhy(it.ref) || t('cal.fromSeries'), it.ref.location_text, it.ref.teacher].filter(Boolean).join(' · ');
}

export default {
  id: 'calendar',
  tab: 'more',
  detail: true,
  title: () => t('nav.calendar'),
  load: ({ fresh }) => load('/api/v1/calendar', { fresh }),
  render(data, request = {}) {
    const templates = new Map((data.recurring_templates || []).map((x) => [x.id, x]));
    const cur = now();
    const items = [
      ...(data.events || []).filter((e) => e.status === 'ACTIVE').map((e) => ({ kind: 'event', at: e.starts_at, end: e.ends_at, title: e.title, ref: e })),
      ...(data.occurrences || []).map((o) => ({ kind: 'occ', at: o.starts_at, end: o.ends_at, title: o.title || templates.get(o.template_id)?.title || o.template_id, ref: o })),
    ].filter((x) => new Date(x.end) >= new Date(cur.getTime() - 86400000)).sort((a, b) => new Date(a.at) - new Date(b.at));
    this._items = items;
    this._deepLinkEvent = request?.event || null;
    const groups = new Map();
    items.forEach((it, i) => {
      const key = dayKey(it.at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([it, i]);
    });
    const agenda = [...groups.entries()].slice(0, 21).map(([key, list]) => `
      <div class="day-group">
        <h3 class="day-title">${esc(fmtDay(`${key}T12:00:00`))}</h3>
        <div class="list">${list.map(([it, i]) => `<button class="row${it.ref.cancelled ? ' cancelled' : ''}" data-action="cal-item" data-index="${i}">
          <span class="row-time"><strong>${esc(fmtTime(it.at))}</strong><small>${esc(fmtTime(it.end))}</small></span>
          <span class="row-main"><strong>${esc(it.title)}</strong><small>${esc(rowNote(it))}</small></span>
          ${it.ref._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
        </button>`).join('')}</div>
      </div>`).join('');
    const series = (data.recurring_templates || []).map((x) => `<button class="card series-card plain" data-action="cal-series" data-id="${esc(x.id)}">
      <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc([ruleText(x.recurrence_rule), fmtDateTime(x.dtstart_local), fmtDuration(x.duration_minutes), x.location_text, x.imported ? t('series.imported') : ''].filter(Boolean).join(' · '))}</small></span>
    </button>`).join('');
    return `
      <div class="quick-actions">
        <button class="button" data-action="cal-new-event">${icon('event')}${esc(t('compose.event'))}</button>
        <button class="button" data-action="cal-new-recurring">${icon('repeat')}${esc(t('compose.recurring'))}</button>
        ${(data.recurring_templates || []).length ? `<button class="button" data-action="cal-holiday">${icon('calendar')}${esc(t('series.holiday'))}</button>` : ''}
      </div>
      <section class="section">${agenda || empty(t('cal.empty'), t('cal.emptyHint'), 'calendar')}</section>
      ${series ? `<section class="section"><div class="section-head"><h2>${esc(t('cal.series'))}</h2></div><div class="stack">${series}</div></section>` : ''}`;
  },
  mount() {
    if (!this._deepLinkEvent) return;
    const event = this._items?.find((item) => item.kind === 'event' && item.ref.id === this._deepLinkEvent);
    this._deepLinkEvent = null;
    if (event) eventSheet(event.ref);
  },
  actions: {
    'cal-item'(el, ctx) {
      const it = ctx.view._items?.[Number(el.dataset.index)];
      if (!it) return;
      if (it.kind === 'event') eventSheet(it.ref);
      else occurrenceSheet(it.ref, (ctx.data.recurring_templates || []).find((x) => x.id === it.ref.template_id));
    },
    'cal-series'(el, ctx) {
      const template = (ctx.data.recurring_templates || []).find((x) => x.id === el.dataset.id);
      if (template) seriesSheet(template);
    },
    'cal-holiday'() { holidaySheet(null); },
    'cal-new-event'() { composers.event(); },
    'cal-new-recurring'() { composers.recurring(); },
  },
};

