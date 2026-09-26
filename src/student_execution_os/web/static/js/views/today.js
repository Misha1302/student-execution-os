import { load } from '../store.js';
import { t, code, fmtTime, fmtDuration, fmtRelative, fmtDateTime, setServerNow, now, sameDay } from '../i18n.js';
import { esc, icon, chip, riskChip, statusClass, statusIcon, empty, sectionHead } from '../ui.js';
import { logProgress, lifecycle, change } from '../actions.js';
import { rescheduleSheet } from '../capture.js';
import { isOpen, hasAlarm, reminderStatusChip } from '../reminders.js';

export function parseWhyNow(value) {
  const out = {};
  for (const part of String(value || '').split(';')) {
    const [k, v] = part.split('=');
    if (k && v) out[k] = v;
  }
  return out;
}

// Plan explanations such as "UNKNOWN_HARD_CUTOFF:task-1" become sentences about the
// task or event they name. Internal codes and ids never reach the screen: a code
// without a sentence becomes a generic one.
export function explainReason(reason, tasks = [], events = []) {
  const [codeName, refId] = String(reason || '').split(':');
  const task = refId ? tasks.find((x) => x.id === refId) : null;
  if (task) {
    const key = `reasonTask.${codeName}`;
    const text = t(key, { title: task.title });
    if (text !== key) return { text, task };
  }
  const event = refId ? events.find((x) => x.id === refId) : null;
  if (event) {
    const key = `reasonEvent.${codeName}`;
    const text = t(key, { title: event.title });
    if (text !== key) return { text, event, codeName };
  }
  const key = `reason.${codeName}`;
  const text = t(key);
  return { text: text !== key ? text : t('reason.other'), task: null };
}

// Plan notes that are information, not a problem to solve.
const INFO_REASONS = /^(OPTIONAL_EVENT_OMITTED|PREFERRED_EVENT_OMITTED|REPLAN_INPUT_CHANGED)/;

export function statusCopy(status, reasons = [], tasks = [], events = []) {
  if (status === 'FEASIBLE') return { text: t('status.copy.FEASIBLE'), task: null };
  const firstReason = (reasons || []).find((r) => !INFO_REASONS.test(r));
  const first = firstReason ? explainReason(firstReason, tasks, events) : null;
  if (status === 'INFEASIBLE') return first?.task || first?.event ? first : { text: t('status.copy.INFEASIBLE'), task: null };
  if (!first) return { text: t('status.copy.UNKNOWN'), task: null };
  return first.task || first.event ? first : { text: t('status.copy.UNKNOWN.reason', { reason: first.text }), task: null };
}

function fixButtons(copy) {
  if (copy.task) return `<button class="button small" data-action="open-task" data-id="${esc(copy.task.id)}">${esc(t('status.fix'))}</button>`;
  if (copy.event && copy.codeName === 'UNSUPPORTED_OPTIONAL_EVENT_POLICY') {
    return `<div class="button-row">
      <button class="button small" data-action="event-attend" data-id="${esc(copy.event.id)}">${esc(t('status.attend'))}</button>
      <button class="button small ghost" data-action="allow-skip-optional">${esc(t('status.allowSkip'))}</button></div>`;
  }
  if (copy.event) return `<button class="button small" data-action="open-event" data-id="${esc(copy.event.id)}">${esc(t('status.fix'))}</button>`;
  return '';
}

export function heroStatus(plan, tasks = [], extra = '') {
  const s = plan.feasibility_status;
  const copy = statusCopy(s, plan.explanations, tasks, plan.canonical_events || []);
  const fix = fixButtons(copy);
  return `<section class="hero-status ${statusClass(s)}" data-status="${esc(s)}">
    <div class="hero-icon ${statusClass(s)}">${icon(statusIcon(s))}</div>
    <div class="hero-copy">
      <h2 class="${statusClass(s)}">${esc(t(`status.title.${s}`))}</h2>
      <p>${esc(copy.text)}</p>
      ${fix}${extra}
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
    return `<div class="banner warn">${icon('alert')}<div><strong>${esc(t('travel.unknown'))}</strong><p>${esc([...new Set(travel.unknown_reasons.map((r) => explainReason(r).text))].join(' · '))}</p></div></div>`;
  }
  if (travel?.infeasible_reasons?.length) {
    return `<div class="banner danger">${icon('alert')}<div><strong>${esc(t('travel.infeasible'))}</strong><p>${esc([...new Set(travel.infeasible_reasons.map((r) => explainReason(r).text))].join(' · '))}</p></div></div>`;
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
      <button class="button ghost" data-action="reschedule-task" data-id="${esc(task.id)}">${esc(t('task.reschedule'))}</button>
    </div>` : ''}
  </article>`;
}

// Open tasks the plan does not put in front of the user right now — so nothing
// disappears from Today when the plan is uncertain, a task is put off or a new
// task was created offline and is not planned yet.
export function soonTasks(data, cur = now()) {
  const planned = new Set((data.next_actions || []).map((a) => a.task_id));
  const unplanned = new Set(data.unplanned_pending || []);
  const horizon = cur.getTime() + 3 * 86400000;
  const due = (x) => new Date(x.actual_cutoff?.at || x.target_at || x.actionable_from || 8.64e15).getTime();
  return (data.tasks || [])
    .filter((x) => x.status === 'ACTIVE' && !planned.has(x.id))
    .filter((x) => unplanned.has(x.id) || due(x) <= horizon || (x.actionable_from && new Date(x.actionable_from).getTime() <= horizon)
      || data.plan?.feasibility_status !== 'FEASIBLE')
    .map((x) => {
      let why;
      if (x.actionable_from && new Date(x.actionable_from) > cur) why = t('soon.deferred', { when: fmtDateTime(x.actionable_from) });
      else if (unplanned.has(x.id)) why = t('soon.notPlanned');
      else if (x.actual_cutoff?.state === 'KNOWN' && x.actual_cutoff.at) why = t('soon.due', { when: fmtDateTime(x.actual_cutoff.at) });
      else if (x.target_at) why = t('soon.target', { when: fmtDateTime(x.target_at) });
      else why = t('soon.anytime');
      return { task: x, why, ready: !(x.actionable_from && new Date(x.actionable_from) > cur) };
    })
    .sort((a, b) => Number(b.ready) - Number(a.ready) || due(a.task) - due(b.task));
}

function fallbackNowCard(item) {
  const task = item.task;
  return `<article class="card now-card" data-action="open-task" data-id="${esc(task.id)}">
    <div class="now-head"><span class="eyebrow">${esc(t('today.suggested'))}</span>${riskChip(task.risk)}</div>
    <h3>${esc(task.title)}</h3>
    <p class="muted">${esc(item.why)}${task.remaining_effort_minutes != null ? ` · ${esc(t('tasks.left', { d: fmtDuration(task.remaining_effort_minutes) }))}` : ''}</p>
    <div class="now-actions">
      ${task.started_at ? `<button class="button primary" data-action="progress" data-id="${esc(task.id)}">${icon('check')}${esc(t('task.logProgress'))}</button>`
        : `<button class="button primary" data-action="start-task" data-id="${esc(task.id)}">${esc(t('today.start'))}</button>`}
      <button class="button ghost" data-action="complete-task" data-id="${esc(task.id)}">${esc(t('lifecycle.complete'))}</button>
      <button class="button ghost" data-action="defer-task" data-id="${esc(task.id)}">${esc(t('today.notNow'))}</button>
      <button class="button ghost" data-action="reschedule-task" data-id="${esc(task.id)}">${esc(t('task.reschedule'))}</button>
    </div>
  </article>`;
}

// Standalone reminders due today (and ones that rang and still wait for an answer).
function todayReminders(reminders = [], cur = now()) {
  const list = reminders.filter((r) => isOpen(r) && (sameDay(new Date(r.remind_at), cur) || r.status === 'FIRED'))
    .sort((a, b) => new Date(a.remind_at) - new Date(b.remind_at));
  if (!list.length) return '';
  return `<section class="section">
    ${sectionHead(t('today.reminders'), `<button class="link" data-nav="tasks">${esc(t('nav.tasks'))}</button>`)}
    <div class="list">${list.map((r) => `<button class="row" data-action="open-reminder" data-kind="REMINDER" data-id="${esc(r.id)}">
      <span class="row-time"><strong>${esc(fmtTime(r.remind_at))}</strong><small>${icon(hasAlarm(r.delivery) ? 'clock' : 'bell')}</small></span>
      <span class="row-main"><strong>${esc(r.title)}</strong>${r.note ? `<small>${esc(r.note)}</small>` : ''}</span>
      ${reminderStatusChip(r)}
    </button>`).join('')}</div>
  </section>`;
}

export default {
  id: 'today',
  tab: 'today',
  title: () => t('nav.today'),
  async load({ fresh }) {
    const [result, reminders] = await Promise.all([
      load('/api/v1/today', { fresh }),
      load('/api/v1/reminders', { fresh }).catch(() => ({ data: [] })),
    ]);
    setServerNow(result.data.now);
    return { ...result, data: { ...result.data, reminders: reminders.data || [] } };
  },
  render(data) {
    const plan = data.plan;
    const tasks = new Map((data.tasks || []).map((x) => [x.id, x]));
    const actions = data.next_actions || [];
    const [first, ...rest] = actions;
    const unhealthy = (data.source_health || []).filter((s) => s.health_status !== 'CURRENT');
    const cur = now();
    const events = (plan.canonical_events || []).filter((e) => new Date(e.ends_at) >= cur && sameDay(new Date(e.starts_at), cur))
      .sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at));
    const soon = soonTasks(data, cur);
    const suggestion = !first ? soon.find((x) => x.ready) : null;
    const later = soon.filter((x) => x !== suggestion);
    const atRisk = (data.tasks || []).filter((x) => ['AT_RISK', 'CRITICAL', 'IMPOSSIBLE', 'OVERDUE'].includes(x.risk?.state));
    const travel = travelCard(data.travel);
    const bounds = boundaries(data);

    const nothingYet = !(data.tasks || []).length && !(data.needs_refinement || []).length;
    const capture = `<button class="capture-cta" data-action="compose">
        <span class="capture-cta-copy"><strong>${esc(t('capture.title'))}</strong><small>${esc(t('capture.ctaHint'))}</small></span>
        <span class="capture-cta-icons">${icon('plus')}</span></button>`;
    return `
      ${unhealthy.length ? `<button class="banner warn" data-nav="evidence">${icon('alert')}<div><strong>${esc(t('today.sourcesStale', { n: unhealthy.length }))}</strong><p>${esc(t('today.sourcesStaleHint'))}</p></div>${icon('chevron')}</button>` : ''}
      ${nothingYet ? `<section class="section">${capture}<p class="help pad">${esc(t('today.firstHint'))}</p></section>` : heroStatus(plan, data.tasks || [])}

      <section class="section ${nothingYet ? 'hidden' : ''}">
        ${sectionHead(t('today.now'))}
        ${first ? nowCard(first, tasks.get(first.task_id), plan) : suggestion ? fallbackNowCard(suggestion) : nothingYet ? '' : empty(
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

      ${later.length ? `<section class="section" data-soon>
        ${sectionHead(t('today.soon'))}
        <div class="list">${later.map((x) => `<div class="row soon-row">
          <button class="row-main plain" data-action="open-task" data-id="${esc(x.task.id)}"><strong>${esc(x.task.title)}</strong><small>${esc(x.why)}</small></button>
          ${x.ready ? `<button class="button small ghost" data-action="start-task" data-id="${esc(x.task.id)}">${esc(t('today.start'))}</button>` : ''}
          <button class="button small ghost" data-action="complete-task" data-id="${esc(x.task.id)}" aria-label="${esc(t('lifecycle.complete'))}">${icon('check')}</button>
        </div>`).join('')}</div>
      </section>` : ''}

      ${data.needs_refinement?.length ? `<section class="section">
        ${sectionHead(t('today.needsRefinement'))}
        <div class="list">${data.needs_refinement.map((x) => `<button class="row" data-action="open-task" data-id="${esc(x.id)}">
          <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(t('today.needsEstimate'))}</small></span>
          ${icon('chevron')}
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

      ${todayReminders(data.reminders, cur)}

      <section class="section">
        ${sectionHead(t('today.events'), `<button class="link" data-nav="calendar">${esc(t('nav.calendar'))}</button>`)}
        ${events.length ? `<div class="list">${events.map((e) => {
          const running = new Date(e.starts_at) <= cur;
          const bits = [running ? t('today.eventNow') : fmtRelative(e.starts_at)];
          if (e.attendance_policy !== 'REQUIRED') bits.push(code('attendance', e.attendance_policy));
          if (e.location_effect?.kind && e.location_effect.kind !== 'NONE') bits.push(code('location', e.location_effect.kind));
          if (e.remind_before_minutes != null) bits.push(t('event.remindShort', { n: e.remind_before_minutes }));
          return `<button class="row${running ? ' current' : ''}" data-action="open-event" data-id="${esc(e.id)}">
          <span class="row-time"><strong>${esc(fmtTime(e.starts_at))}</strong><small>${esc(fmtTime(e.ends_at))}</small></span>
          <span class="row-main"><strong>${esc(e.title)}</strong><small>${esc(bits.join(' · '))}</small></span>
          ${running ? chip(t('today.eventNowChip'), 'accent') : ''}
        </button>`;
        }).join('')}</div>` : `<p class="muted pad">${esc(t('today.noEvents'))}</p>`}
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
      if (task) await change('task.start', task.id, {}, { success: t('today.started') });
    },
    async 'complete-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) await lifecycle(task.id, task.version, 'complete');
    },
    'reschedule-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) rescheduleSheet(task);
    },
    async 'defer-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (!task) return;
      const until = new Date(now().getTime() + 60 * 60000).toISOString();
      await change('task.defer', task.id, { until }, { success: t('today.deferred', { when: fmtTime(until) }) });
    },
  },
};
