import { load } from '../store.js';
import { api } from '../api.js';
import { t, code, fmtTime, fmtDuration, fmtRelative, fmtDateTime, setServerNow, now, sameDay } from '../i18n.js';
import { esc, icon, chip, riskChip, statusClass, statusIcon, empty, sectionHead } from '../ui.js';
import { logProgress, lifecycle, mutate } from '../actions.js';

export function parseWhyNow(value) {
  const out = {};
  for (const part of String(value || '').split(';')) {
    const [k, v] = part.split('=');
    if (k && v) out[k] = v;
  }
  return out;
}

export function statusCopy(status, reasons = []) {
  const reason = reasons[0] ? code('reason', reasons[0]) : '';
  if (status === 'FEASIBLE') return t('status.copy.FEASIBLE');
  if (status === 'INFEASIBLE') return reason ? t('status.copy.INFEASIBLE.reason', { reason }) : t('status.copy.INFEASIBLE');
  return reason ? t('status.copy.UNKNOWN.reason', { reason }) : t('status.copy.UNKNOWN');
}

export function heroStatus(plan, extra = '') {
  const s = plan.feasibility_status;
  return `<section class="hero-status ${statusClass(s)}" data-status="${esc(s)}">
    <div class="hero-icon ${statusClass(s)}">${icon(statusIcon(s))}</div>
    <div class="hero-copy">
      <p class="eyebrow">${esc(t('status.eyebrow'))} · <span class="mono">${esc(s)}</span></p>
      <h2 class="${statusClass(s)}">${esc(t(`status.title.${s}`))}</h2>
      <p>${esc(statusCopy(s, plan.explanations))}</p>
      ${extra}
    </div>
  </section>`;
}

function boundaries(data) {
  const items = [];
  for (const task of data.tasks || []) {
    if (task.status !== 'ACTIVE') continue;
    if (task.risk?.latest_safe_start) items.push({ at: task.risk.latest_safe_start, kind: 'lss', title: task.title, id: task.id });
    if (task.actual_cutoff?.at) items.push({ at: task.actual_cutoff.at, kind: 'cutoff', title: task.title, id: task.id });
    else if (task.target_at) items.push({ at: task.target_at, kind: 'target', title: task.title, id: task.id });
  }
  const cur = now();
  return items.filter((b) => new Date(b.at) >= cur).sort((a, b) => new Date(a.at) - new Date(b.at)).slice(0, 5);
}

function travelCard(travel) {
  if (travel?.unknown_reasons?.length) {
    return `<div class="banner warn">${icon('alert')}<div><strong>${esc(t('travel.unknown'))}</strong><p>${esc(travel.unknown_reasons.map((r) => code('reason', r)).join(' · '))}</p></div></div>`;
  }
  if (travel?.infeasible_reasons?.length) {
    return `<div class="banner danger">${icon('alert')}<div><strong>${esc(t('travel.infeasible'))}</strong><p>${esc(travel.infeasible_reasons.map((r) => code('reason', r)).join(' · '))}</p></div></div>`;
  }
  const upcoming = (travel?.transitions || []).filter((x) => new Date(x.travel_ends_at) >= now());
  return upcoming.map((x) => `
    <article class="card travel-card">
      <div class="travel-leave">
        <span class="eyebrow">${esc(t('travel.leaveBy'))}</span>
        <strong class="big-time">${esc(fmtTime(x.latest_safe_departure))}</strong>
        <small>${esc(fmtRelative(x.latest_safe_departure))}</small>
      </div>
      <div class="travel-route">
        <div class="route">${icon('route')}<strong>${esc(x.origin)}</strong><span>→</span><strong>${esc(x.destination)}</strong></div>
        <dl class="mini-kv">
          <div><dt>${esc(t('travel.safeDuration'))}</dt><dd>${esc(fmtDuration(x.safe_duration_minutes))}</dd></div>
          <div><dt>${esc(t('travel.buffer'))}</dt><dd>${esc(fmtDuration(x.arrival_requirement_minutes))}</dd></div>
          <div><dt>${esc(t('travel.arrive'))}</dt><dd>${esc(fmtTime(x.travel_ends_at))}</dd></div>
        </dl>
      </div>
    </article>`).join('');
}

function nowCard(action, task, plan) {
  const why = parseWhyNow(action.why_now);
  const start = why.PLAN_START || plan.blocks.find((b) => b.type === 'WORK' && b.obligation_id === action.task_id)?.starts_at;
  const startsNow = start && new Date(start) <= now();
  return `<article class="card now-card" data-action="open-task" data-id="${esc(action.task_id)}">
    <div class="now-head">
      <span class="eyebrow">${esc(startsNow ? t('today.nowEyebrow') : t('today.nextEyebrow', { time: fmtTime(start) }))}</span>
      ${riskChip({ state: why.RISK || task?.risk?.state })}
    </div>
    <h3>${esc(action.what)}</h3>
    <p class="muted">${esc(t('today.block', { d: fmtDuration(action.recommended_duration_minutes) }))}${action.relevant_at ? ` · ${esc(t('today.due', { when: fmtDateTime(action.relevant_at) }))}` : ''}</p>
    ${task ? `<div class="now-actions">
      <button class="button primary" data-action="start-task" data-id="${esc(task.id)}">${esc(t('today.start'))}</button>
      <button class="button primary" data-action="progress" data-id="${esc(task.id)}" data-minutes="${esc(action.recommended_duration_minutes)}">${icon('check')}${esc(t('today.didBlock', { d: fmtDuration(action.recommended_duration_minutes) }))}</button>
      <button class="button ghost" data-action="complete-task" data-id="${esc(task.id)}">${esc(t('lifecycle.complete'))}</button>
      <button class="button ghost" data-action="defer-task" data-id="${esc(task.id)}">${esc(t('today.notNow'))}</button>
      <button class="button ghost" data-action="open-task" data-id="${esc(task.id)}">${esc(t('common.open'))}</button>
    </div>` : ''}
  </article>`;
}

export default {
  id: 'today',
  tab: 'today',
  title: () => t('nav.today'),
  async load({ fresh }) {
    const result = await load('/api/v1/today', { fresh });
    setServerNow(result.data.now);
    return result;
  },
  render(data) {
    const plan = data.plan;
    const tasks = new Map((data.tasks || []).map((x) => [x.id, x]));
    const actions = data.next_actions || [];
    const [first, ...rest] = actions;
    const unhealthy = (data.source_health || []).filter((s) => s.health_status !== 'CURRENT');
    const cur = now();
    const events = (plan.canonical_events || []).filter((e) => new Date(e.ends_at) >= cur && sameDay(new Date(e.starts_at), cur));
    const atRisk = (data.tasks || []).filter((x) => ['AT_RISK', 'CRITICAL', 'IMPOSSIBLE', 'OVERDUE'].includes(x.risk?.state));
    const travel = travelCard(data.travel);
    const bounds = boundaries(data);

    return `
      ${unhealthy.length ? `<button class="banner warn" data-nav="evidence">${icon('alert')}<div><strong>${esc(t('today.sourcesStale', { n: unhealthy.length }))}</strong><p>${esc(t('today.sourcesStaleHint'))}</p></div>${icon('chevron')}</button>` : ''}
      ${heroStatus(plan)}

      <section class="section">
        ${sectionHead(t('today.now'))}
        ${first ? nowCard(first, tasks.get(first.task_id), plan) : empty(
          plan.feasibility_status === 'FEASIBLE' ? t('today.nothing') : t('today.resolveFirst'),
          plan.feasibility_status === 'FEASIBLE' ? t('today.nothingHint') : t('today.resolveFirstHint'),
          plan.feasibility_status === 'FEASIBLE' ? 'check' : 'question',
        )}
      </section>

      ${rest.length ? `<section class="section">
        ${sectionHead(t('today.upNext'), `<button class="link" data-nav="plan">${esc(t('today.fullPlan'))}</button>`)}
        <div class="list">${rest.map((a) => {
          const why = parseWhyNow(a.why_now);
          return `<button class="row" data-action="open-task" data-id="${esc(a.task_id)}">
            <span class="row-time"><strong>${esc(fmtTime(why.PLAN_START))}</strong><small>${esc(fmtDuration(a.recommended_duration_minutes))}</small></span>
            <span class="row-main"><strong>${esc(a.what)}</strong></span>
            ${riskChip({ state: why.RISK })}
          </button>`;
        }).join('')}</div>
      </section>` : ''}

      ${data.needs_refinement?.length ? `<section class="section">
        ${sectionHead(t('today.needsRefinement'))}
        <div class="list">${data.needs_refinement.map((x) => `<button class="row" data-action="open-task" data-id="${esc(x.id)}">
          <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(t('today.needsEstimate'))}</small></span>
          ${chip(code('status', 'DRAFT'), 'warn')}
        </button>`).join('')}</div>
      </section>` : ''}

      ${travel ? `<section class="section">${sectionHead(t('today.travel'))}${travel}</section>` : ''}

      ${atRisk.length ? `<section class="section">
        ${sectionHead(t('today.atRisk'))}
        <div class="list">${atRisk.map((x) => `<button class="row" data-action="open-task" data-id="${esc(x.id)}">
          <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(t('tasks.left', { d: fmtDuration(x.remaining_effort_minutes) }))}${x.risk?.latest_safe_start ? ` · ${esc(t('tasks.startBy', { when: fmtDateTime(x.risk.latest_safe_start) }))}` : ''}</small></span>
          ${riskChip(x.risk)}
        </button>`).join('')}</div>
      </section>` : ''}

      <section class="section">
        ${sectionHead(t('today.events'), `<button class="link" data-nav="calendar">${esc(t('nav.calendar'))}</button>`)}
        ${events.length ? `<div class="list">${events.map((e) => `<button class="row" data-action="open-event" data-id="${esc(e.id)}">
          <span class="row-time"><strong>${esc(fmtTime(e.starts_at))}</strong><small>${esc(fmtTime(e.ends_at))}</small></span>
          <span class="row-main"><strong>${esc(e.title)}</strong><small>${esc(code('attendance', e.attendance_policy))}${e.location_effect.kind !== 'NONE' ? ` · ${esc(code('location', e.location_effect.kind))}` : ''}</small></span>
          ${chip(code('own', 'CANONICAL'), 'canonical')}
        </button>`).join('')}</div>` : `<p class="muted pad">${esc(t('today.noEvents'))}</p>`}
      </section>

      <section class="section">
        ${sectionHead(t('today.boundaries'))}
        ${bounds.length ? `<div class="list">${bounds.map((b) => `<button class="row" data-action="open-task" data-id="${esc(b.id)}">
          <span class="row-icon tone-${b.kind === 'cutoff' ? 'danger' : b.kind === 'lss' ? 'warn' : 'accent'}">${icon(b.kind === 'cutoff' ? 'flag' : b.kind === 'lss' ? 'clock' : 'task')}</span>
          <span class="row-main"><strong>${esc(b.title)}</strong><small>${esc(t(`bound.${b.kind}`))} · ${esc(fmtDateTime(b.at))}</small></span>
          <span class="row-aside">${esc(fmtRelative(b.at))}</span>
        </button>`).join('')}</div>` : `<p class="muted pad">${esc(t('today.noBoundaries'))}</p>`}
      </section>`;
  },
  actions: {
    progress(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) logProgress(task, el.dataset.minutes);
    },
    async 'start-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) await mutate(() => api(`/api/v1/tasks/${encodeURIComponent(task.id)}/start`, { method: 'POST', body: { expected_version: task.version } }), { success: t('today.started') });
    },
    async 'complete-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) await lifecycle(task.id, task.version, 'complete');
    },
    async 'defer-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (!task) return;
      const until = new Date(now().getTime() + 60 * 60000).toISOString();
      await mutate(() => api(`/api/v1/tasks/${encodeURIComponent(task.id)}/defer`, { method: 'POST', body: { expected_version: task.version, until } }), { success: t('today.deferred') });
    },
  },
};
