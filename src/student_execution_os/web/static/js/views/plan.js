import { load } from '../store.js';
import { t, code, fmtTime, fmtDay, fmtDuration, fmtDateTime, dayKey, now, setServerNow } from '../i18n.js';
import { esc, icon, chip, kv, empty, openSheet, chipGroup } from '../ui.js';
import { heroStatus, explainReason } from './today.js';

const BLOCK_CLASS = {
  WORK: 'work', EVENT_PROJECTION: 'event-projection', TRAVEL_TRANSITION: 'travel', BUFFER: 'buffer',
};

let selectedDay = null;

function rangeNav(active) {
  return `<nav class="day-strip" aria-label="${esc(t('plan.range'))}">
    ${[['today', t('nav.today')], ['week', t('plan.week')], ['month', t('plan.month')]].map(([id, label]) =>
      `<a class="chip ${active === id ? 'selected' : ''}" href="#/plan${id === 'today' ? '' : `?step=${id}`}">${esc(label)}</a>`).join('')}
  </nav>`;
}

function outlookView(data) {
  const month = data.range === 'month';
  return `${rangeNav(data.range)}
    <section class="section">
      ${data.uncertainty_reasons?.length ? `<div class="banner warn">${icon('alert')}<div><strong>${esc(t('plan.provisional'))}</strong><p>${esc([...new Set(data.uncertainty_reasons.map((x) => explainReason(x).text))].join(' · '))}</p></div></div>` : ''}
      <div class="list">${data.days.map((day) => `<article class="row">
        <span class="row-time"><strong>${esc(fmtDay(`${day.date}T12:00:00`))}</strong><small>${esc(fmtDuration(day.planning_capacity_minutes))}</small></span>
        <span class="row-main"><strong>${esc(t('plan.free', { d: fmtDuration(day.free_capacity_minutes) }))}</strong>
          <small>${esc(t('plan.load', { d: fmtDuration(day.planned_load_minutes) }))}${day.deadlines.length ? ` · ${esc(t('plan.deadlines', { n: day.deadlines.length }))}` : ''}</small></span>
        ${day.provisional ? chip(t('plan.provisional'), 'warn') : day.risk.length || day.shortfall_minutes ? chip(t('plan.riskDay'), 'danger') : ''}
      </article>`).join('')}</div>
    </section>
    ${month ? `<section class="section"><div class="list">${data.weeks.map((week) => `<a class="row" href="#/plan?step=week">
      <span class="row-main"><strong>${esc(t('plan.weekOf', { date: fmtDay(`${week.starts_on}T12:00:00`) }))}</strong><small>${esc(t('plan.free', { d: fmtDuration(week.free_capacity_minutes) }))}</small></span>
      ${week.risk_days.length ? chip(t('plan.riskDays', { n: week.risk_days.length }), 'warn') : ''}</a>`).join('')}</div></section>` : ''}`;
}

export function agendaItems(plan) {
  const items = [];
  for (const e of plan.canonical_events || []) {
    items.push({ kind: 'EVENT', cls: 'canonical', ownership: 'CANONICAL', starts_at: e.starts_at, ends_at: e.ends_at, label: e.title, detail: code('attendance', e.attendance_policy), ref: e });
  }
  for (const c of plan.constraints || []) {
    items.push({ kind: c.type, cls: 'constraint', ownership: 'CANONICAL', starts_at: c.starts_at, ends_at: c.ends_at, label: code('constraint', c.type), detail: c.reason || '', ref: c });
  }
  for (const o of plan.off_hours || []) {
    items.push({ kind: 'OFF_HOURS', cls: 'sleep', ownership: 'CANONICAL', starts_at: o.starts_at, ends_at: o.ends_at, label: t('plan.offHours'), detail: t('plan.offHoursHelp'), ref: o });
  }
  const shownEvents = new Set((plan.canonical_events || []).map((e) => e.id));
  for (const b of plan.blocks || []) {
    // An EVENT_PROJECTION of an Event already listed as a canonical fact would only repeat the
    // same interval; the canonical row stays, the projection is folded into it.
    if (b.type === 'EVENT_PROJECTION' && shownEvents.has(b.source_event_id || b.obligation_id)) continue;
    items.push({ kind: b.type, cls: BLOCK_CLASS[b.type] || 'buffer', ownership: 'DERIVED', starts_at: b.starts_at, ends_at: b.ends_at, label: b.label || code('block', b.type), detail: code('block', b.type), ref: b });
  }
  return items.sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at) || (a.ownership === 'CANONICAL' ? -1 : 1));
}

function openItem(item) {
  if (item.kind === 'EVENT') { import('../events.js').then(({ eventSheet }) => eventSheet(item.ref)); return; }
  const b = item.ref;
  const derived = item.ownership === 'DERIVED';
  openSheet({
    eyebrow: derived ? code('block', item.kind) : item.kind === 'EVENT' ? t('legend.event') : code('constraint', item.kind),
    title: item.label,
    body: `<p>${chip(`${fmtTime(item.starts_at)}–${fmtTime(item.ends_at)}`)}</p>
      <dl class="kv-list">
        ${kv(t('plan.starts'), fmtDateTime(item.starts_at))}
        ${kv(t('plan.ends'), fmtDateTime(item.ends_at))}
        ${kv(t('plan.duration'), fmtDuration((new Date(item.ends_at) - new Date(item.starts_at)) / 60000))}
        ${derived ? kv(t('plan.why'), code('explain', b.explanation)) : ''}
        ${b.reason && item.kind !== 'OFF_HOURS' ? kv(t('plan.reason'), b.reason) : ''}
      </dl>
      ${derived ? `<p class="help">${esc(t('plan.derivedHelp'))}</p>` : ''}
      ${derived && b.obligation_id ? `<button type="button" class="button ghost wide" data-action="open-task" data-id="${esc(b.obligation_id)}" data-close-sheet>${esc(t('plan.openTask'))}</button>` : ''}`,
  });
}

const DAY_MS = 86400000;

function localDays(count, from = now()) {
  const start = new Date(from);
  start.setHours(12, 0, 0, 0);
  return Array.from({ length: count }, (_, i) => dayKey(new Date(start.getTime() + i * DAY_MS)));
}

function dayBounds(key) {
  const [y, m, d] = key.split('-').map(Number);
  return [new Date(y, m - 1, d), new Date(y, m - 1, d + 1)];
}

function moveDay(ctx, step) {
  const days = ctx.view._days || [];
  const next = days[days.indexOf(selectedDay) + step];
  if (!next) return;
  selectedDay = next;
  ctx.rerender();
}

export default {
  id: 'plan',
  tab: 'plan',
  title: () => t('nav.plan'),
  async load({ fresh, query }) {
    const range = query?.step;
    if (range === 'week' || range === 'month') return load(`/api/v1/outlook?range=${range}`, { fresh });
    try {
      const result = await load('/api/v1/plan/agenda?days=7', { fresh });
      setServerNow(result.data.now);
      return { ...result, data: { ...result.data.plan, tasks: result.data.tasks || [], agendaDays: result.data.days } };
    } catch (err) {
      // Offline with only Today cached (or an older server): show what Today knows.
      const result = await load('/api/v1/today', { fresh });
      setServerNow(result.data.now);
      return { ...result, data: { ...result.data.plan, tasks: result.data.tasks || [], agendaDays: 2 } };
    }
  },
  render(plan) {
    if (plan.range === 'week' || plan.range === 'month') return outlookView(plan);
    const items = agendaItems(plan);
    const today = dayKey(now());
    const days = localDays(Math.max(1, Number(plan.agendaDays) || 7));
    if (!days.includes(selectedDay)) selectedDay = today;
    this._days = days;
    const [dayStart, dayEnd] = dayBounds(selectedDay);
    // Everything that touches the day, so a night block from yesterday 22:00 shows in the morning.
    const visible = items.filter((i) => new Date(i.starts_at) < dayEnd && new Date(i.ends_at) > dayStart);
    const cur = now();
    let nowPlaced = selectedDay !== today;
    const rows = visible.map((item, index) => {
      let marker = '';
      if (!nowPlaced && new Date(item.starts_at) > cur) {
        nowPlaced = true;
        marker = `<div class="now-line"><span>${esc(t('plan.now', { time: fmtTime(cur) }))}</span></div>`;
      }
      const minutes = Math.max(5, (new Date(item.ends_at) - new Date(item.starts_at)) / 60000);
      const clipped = Math.max(5, (Math.min(new Date(item.ends_at), dayEnd) - Math.max(new Date(item.starts_at), dayStart)) / 60000);
      return `${marker}<button class="agenda-item ${item.cls}" data-action="plan-item" data-index="${index}" data-span="${Math.min(6, 1 + clipped / 60).toFixed(2)}">
        <span class="agenda-time"><strong>${esc(fmtTime(item.starts_at))}</strong><small>${esc(fmtTime(item.ends_at))}</small></span>
        <span class="agenda-bar" aria-hidden="true"></span>
        <span class="agenda-copy"><strong>${esc(item.label)}</strong><small>${esc(item.detail)} · ${esc(fmtDuration(minutes))}</small></span>
      </button>`;
    }).join('');
    this._visible = visible;
    const index = days.indexOf(selectedDay);
    const dayOptions = days.map((d) => [d, fmtDay(`${d}T12:00:00`)]);
    return `${rangeNav('today')}
      ${heroStatus(plan, plan.tasks)}
      <section class="section">
        <div class="day-nav">
          <button class="icon-button" data-action="plan-prev" aria-label="${esc(t('plan.prevDay'))}" ${index <= 0 ? 'disabled' : ''}>‹</button>
          <div class="day-strip scroll-x">${chipGroup('plan-day', dayOptions, selectedDay)}</div>
          <button class="icon-button" data-action="plan-next" aria-label="${esc(t('plan.nextDay'))}" ${index >= days.length - 1 ? 'disabled' : ''}>›</button>
        </div>
        <div class="legend">
          <span><i class="dot canonical"></i>${esc(t('legend.event'))}</span>
          <span><i class="dot work"></i>${esc(t('legend.work'))}</span>
          <span><i class="dot constraint"></i>${esc(t('legend.constraint'))}</span>
          <span><i class="dot sleep"></i>${esc(t('legend.sleep'))}</span>
          <span><i class="dot travel"></i>${esc(t('legend.travel'))}</span>
        </div>
        <div class="agenda mobile-agenda" data-swipe-days>${rows || empty(t('plan.emptyDay'), t('plan.emptyDayHint'), 'plan')}${!nowPlaced ? `<div class="now-line"><span>${esc(t('plan.now', { time: fmtTime(cur) }))}</span></div>` : ''}</div>
        <p class="help pad">${esc(t('plan.swipeHint'))}</p>
      </section>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name !== 'plan-day') return;
      selectedDay = e.detail.value;
      ctx.rerender();
    });
    root.querySelector('.day-strip .chip-toggle.on')?.scrollIntoView?.({ block: 'nearest', inline: 'center' });
    // Horizontal swipe on the day's agenda moves to the previous/next day.
    const area = root.querySelector('[data-swipe-days]');
    let start = null;
    area?.addEventListener('touchstart', (e) => { start = { x: e.touches[0].clientX, y: e.touches[0].clientY }; }, { passive: true });
    area?.addEventListener('touchend', (e) => {
      if (!start) return;
      const dx = e.changedTouches[0].clientX - start.x;
      const dy = e.changedTouches[0].clientY - start.y;
      start = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) moveDay(ctx, dx < 0 ? 1 : -1);
    });
  },
  actions: {
    'plan-item'(el, ctx) {
      const item = ctx.view._visible?.[Number(el.dataset.index)];
      if (item) openItem(item);
    },
    'plan-prev'(_el, ctx) { moveDay(ctx, -1); },
    'plan-next'(_el, ctx) { moveDay(ctx, 1); },
  },
};
