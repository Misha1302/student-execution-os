import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtTime, fmtDay, fmtDuration, fmtDateTime, dayKey, now } from '../i18n.js';
import { esc, icon, chip, ownershipChip, kv, empty, openSheet, localInputValue, isoFromLocalInput, setBusy } from '../ui.js';
import { lifecycle, mutate } from '../actions.js';
import { composers } from '../compose.js';

function eventSheet(e) {
  const active = e.status === 'ACTIVE';
  const dialog = openSheet({
    eyebrow: e.location_effect.kind === 'MOVE' ? t('cal.bookedMove') : t('cal.fixedEvent'),
    title: e.title,
    body: `<p>${ownershipChip('CANONICAL')} ${chip(code('status', e.status), active ? 'ok' : 'muted')}</p>
      <dl class="kv-list">
        ${kv(t('plan.starts'), fmtDateTime(e.starts_at))}
        ${kv(t('plan.ends'), fmtDateTime(e.ends_at))}
        ${kv(t('form.attendance'), code('attendance', e.attendance_policy))}
        ${kv(t('form.location'), code('location', e.location_effect.kind))}
        ${e.arrival_requirement_minutes ? kv(t('cal.arrival'), fmtDuration(e.arrival_requirement_minutes)) : ''}
      </dl>
      ${active ? `<div class="form">
        <label class="field"><span>${esc(t('cal.moveTo'))}</span><input type="datetime-local" data-f="start" value="${esc(localInputValue(e.starts_at))}"></label>
      </div>` : ''}`,
    actions: active
      ? `<button type="button" class="button danger ghost" data-cancel>${esc(t('lifecycle.cancel'))}</button>
         <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`
      : `<button type="button" class="button" data-reopen>${esc(t('lifecycle.reopen'))}</button>`,
  });
  dialog.querySelector('[data-cancel]')?.addEventListener('click', () => { dialog.close(); lifecycle(e.id, e.version, 'cancel', { title: e.title }); });
  dialog.querySelector('[data-reopen]')?.addEventListener('click', () => { dialog.close(); lifecycle(e.id, e.version, 'reopen'); });
  dialog.querySelector('[data-save]')?.addEventListener('click', async (ev) => {
    const startsAt = isoFromLocalInput(dialog.querySelector('[data-f="start"]').value);
    if (!startsAt) return;
    const duration = new Date(e.ends_at) - new Date(e.starts_at);
    setBusy(ev.currentTarget, true);
    await mutate(() => api(`/api/v1/events/${encodeURIComponent(e.id)}`, {
      method: 'PATCH',
      body: { expected_version: e.version, starts_at: startsAt, ends_at: new Date(new Date(startsAt).getTime() + duration).toISOString() },
    }), { success: t('cal.moved') });
    dialog.close('saved');
  });
}

function occurrenceSheet(o, template) {
  const dialog = openSheet({
    eyebrow: t('cal.occurrence'),
    title: template?.title || o.template_id,
    body: `<p>${ownershipChip('DERIVED_OCCURRENCE')} ${o.override_id ? chip(t('cal.overridden'), 'warn') : ''}</p>
      <dl class="kv-list">
        ${kv(t('plan.starts'), fmtDateTime(o.starts_at))}
        ${kv(t('plan.ends'), fmtDateTime(o.ends_at))}
        ${template ? kv(t('cal.rule'), `${template.recurrence_rule} · ${template.timezone_name}`) : ''}
        ${kv(t('cal.identity'), o.original_recurrence_id)}
      </dl>
      <p class="help">${esc(t('cal.occurrenceHelp'))}</p>`,
    actions: `<button type="button" class="button danger ghost" data-skip>${esc(t('cal.skip'))}</button>`,
  });
  dialog.querySelector('[data-skip]').addEventListener('click', async (ev) => {
    setBusy(ev.currentTarget, true);
    await mutate(() => api(
      `/api/v1/recurrence/templates/${encodeURIComponent(o.template_id)}/occurrences/${encodeURIComponent(o.original_recurrence_id)}/override`,
      { method: 'POST', body: { action: 'CANCEL', expected_version: 0 } },
    ), { success: t('cal.skipped') });
    dialog.close('saved');
  });
}

export default {
  id: 'calendar',
  tab: 'more',
  detail: true,
  title: () => t('nav.calendar'),
  load: ({ fresh }) => load('/api/v1/calendar', { fresh }),
  render(data) {
    const templates = new Map((data.recurring_templates || []).map((x) => [x.id, x]));
    const cur = now();
    const items = [
      ...(data.events || []).filter((e) => e.status === 'ACTIVE').map((e) => ({ kind: 'event', at: e.starts_at, end: e.ends_at, title: e.title, ref: e })),
      ...(data.occurrences || []).filter((o) => !o.cancelled).map((o) => ({ kind: 'occ', at: o.starts_at, end: o.ends_at, title: templates.get(o.template_id)?.title || o.template_id, ref: o })),
    ].filter((x) => new Date(x.end) >= new Date(cur.getTime() - 86400000)).sort((a, b) => new Date(a.at) - new Date(b.at));
    this._items = items;
    const groups = new Map();
    items.forEach((it, i) => {
      const key = dayKey(it.at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([it, i]);
    });
    const agenda = [...groups.entries()].slice(0, 21).map(([key, list]) => `
      <div class="day-group">
        <h3 class="day-title">${esc(fmtDay(`${key}T12:00:00`))}</h3>
        <div class="list">${list.map(([it, i]) => `<button class="row" data-action="cal-item" data-index="${i}">
          <span class="row-time"><strong>${esc(fmtTime(it.at))}</strong><small>${esc(fmtTime(it.end))}</small></span>
          <span class="row-main"><strong>${esc(it.title)}</strong><small>${esc(it.kind === 'event' ? code('attendance', it.ref.attendance_policy) : t('cal.fromSeries'))}</small></span>
          ${it.kind === 'event' ? ownershipChip('CANONICAL') : ownershipChip('DERIVED_OCCURRENCE')}
        </button>`).join('')}</div>
      </div>`).join('');
    const series = (data.recurring_templates || []).map((x) => `<div class="card series-card">
      <div class="row-main"><strong>${esc(x.title)}</strong><small>${esc(x.dtstart_local)} · ${esc(fmtDuration(x.duration_minutes))}</small></div>
      <p class="mono small">${esc(x.recurrence_rule)} · ${esc(x.timezone_name)}</p>
      ${ownershipChip('CANONICAL_RULE')}
    </div>`).join('');
    return `
      <div class="quick-actions">
        <button class="button" data-action="cal-new-event">${icon('event')}${esc(t('compose.event'))}</button>
        <button class="button" data-action="cal-new-recurring">${icon('repeat')}${esc(t('compose.recurring'))}</button>
      </div>
      <section class="section">${agenda || empty(t('cal.empty'), t('cal.emptyHint'), 'calendar')}</section>
      ${series ? `<section class="section"><div class="section-head"><h2>${esc(t('cal.series'))}</h2></div><div class="stack">${series}</div></section>` : ''}`;
  },
  actions: {
    'cal-item'(el, ctx) {
      const it = ctx.view._items?.[Number(el.dataset.index)];
      if (!it) return;
      if (it.kind === 'event') eventSheet(it.ref);
      else occurrenceSheet(it.ref, (ctx.data.recurring_templates || []).find((x) => x.id === it.ref.template_id));
    },
    'cal-new-event'() { composers.event(); },
    'cal-new-recurring'() { composers.recurring(); },
  },
};

export { eventSheet };
