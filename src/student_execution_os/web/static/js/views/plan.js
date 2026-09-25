import { load } from '../store.js';
import { t, code, fmtTime, fmtDay, fmtDuration, fmtDateTime, dayKey, now, setServerNow } from '../i18n.js';
import { esc, icon, chip, kv, empty, openSheet, chipGroup } from '../ui.js';
import { heroStatus } from './today.js';

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
      ${data.uncertainty_reasons?.length ? `<div class="banner warn">${icon('alert')}<div><strong>${esc(t('plan.provisional'))}</strong><p>${esc(data.uncertainty_reasons.map((x) => code('reason', x)).join(' · '))}</p></div></div>` : ''}
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
        ${b.reason ? kv(t('plan.reason'), b.reason) : ''}
      </dl>
      ${derived ? `<p class="help">${esc(t('plan.derivedHelp'))}</p>` : ''}
      ${derived && b.obligation_id ? `<button type="button" class="button ghost wide" data-action="open-task" data-id="${esc(b.obligation_id)}" data-close-sheet>${esc(t('plan.openTask'))}</button>` : ''}`,
  });
}

export default {
  id: 'plan',
  tab: 'plan',
  title: () => t('nav.plan'),
  async load({ fresh, query }) {
    const range = query?.step;
    if (range === 'week' || range === 'month') return load(`/api/v1/outlook?range=${range}`, { fresh });
    const result = await load('/api/v1/today', { fresh });
    setServerNow(result.data.now);
    return { ...result, data: { ...result.data.plan, tasks: result.data.tasks || [] } };
  },
  render(plan) {
    if (plan.range === 'week' || plan.range === 'month') return outlookView(plan);
    const items = agendaItems(plan);
    const days = [...new Set(items.map((i) => dayKey(i.starts_at)))];
    const today = dayKey(now());
    if (!days.includes(today)) days.unshift(today);
    if (!days.includes(selectedDay)) selectedDay = days.includes(today) ? today : days[0];
    const visible = items.filter((i) => dayKey(i.starts_at) === selectedDay);
    const cur = now();
    let nowPlaced = selectedDay !== today;
    const rows = visible.map((item, index) => {
      let marker = '';
      if (!nowPlaced && new Date(item.starts_at) > cur) {
        nowPlaced = true;
        marker = `<div class="now-line"><span>${esc(t('plan.now', { time: fmtTime(cur) }))}</span></div>`;
      }
      const minutes = Math.max(5, (new Date(item.ends_at) - new Date(item.starts_at)) / 60000);
      return `${marker}<button class="agenda-item ${item.cls}" data-action="plan-item" data-index="${index}" data-span="${Math.min(6, 1 + minutes / 60).toFixed(2)}">
        <span class="agenda-time"><strong>${esc(fmtTime(item.starts_at))}</strong><small>${esc(fmtTime(item.ends_at))}</small></span>
        <span class="agenda-bar" aria-hidden="true"></span>
        <span class="agenda-copy"><strong>${esc(item.label)}</strong><small>${esc(item.detail)} · ${esc(fmtDuration(minutes))}</small></span>
      </button>`;
    }).join('');
    this._visible = visible;
    const dayOptions = days.map((d) => [d, fmtDay(`${d}T12:00:00`)]);
    return `${rangeNav('today')}
      ${heroStatus(plan, plan.tasks)}
      <section class="section">
        <div class="day-strip">${chipGroup('plan-day', dayOptions, selectedDay)}</div>
        <div class="legend">
          <span><i class="dot canonical"></i>${esc(t('legend.event'))}</span>
          <span><i class="dot constraint"></i>${esc(t('legend.constraint'))}</span>
          <span><i class="dot work"></i>${esc(t('legend.work'))}</span>
          <span><i class="dot travel"></i>${esc(t('legend.travel'))}</span>
          <span><i class="dot buffer"></i>${esc(t('legend.buffer'))}</span>
        </div>
        <div class="agenda mobile-agenda">${rows || empty(t('plan.emptyDay'), t('plan.emptyDayHint'), 'plan')}${!nowPlaced ? `<div class="now-line"><span>${esc(t('plan.now', { time: fmtTime(cur) }))}</span></div>` : ''}</div>
      </section>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name !== 'plan-day') return;
      selectedDay = e.detail.value;
      ctx.rerender();
    });
  },
  actions: {
    'plan-item'(el, ctx) {
      const item = ctx.view._visible?.[Number(el.dataset.index)];
      if (item) openItem(item);
    },
  },
};
