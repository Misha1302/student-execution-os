import { load } from '../store.js';
import { t, code, fmtTime, fmtDuration, fmtRelative, fmtDateTime, setServerNow, now, sameDay } from '../i18n.js';
import { esc, icon, chip, riskChip, statusClass, statusIcon, empty, sectionHead, openSheet, setBusy } from '../ui.js';
import { logProgress, lifecycle, change } from '../actions.js';
import { rescheduleSheet } from '../task-sheets.js';
import { isOpen, hasAlarm, reminderStatusChip } from '../reminders.js';
import { upcomingItems, currentEvent as runningEvent } from '../upcoming.js';
import { todayCheckinsSection } from '../checkins.js';
import { executionCard, mountExecutionTimers, startExecution, pauseExecution, resumeExecution, finishExecution, reviewLongExecution } from '../execution.js';

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
const INFO_REASONS = /^(OPTIONAL_EVENT_OMITTED|PREFERRED_EVENT_OMITTED|REPLAN_INPUT_CHANGED|QUOTA_DOES_NOT_FIT)/;

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

// Travel the plan cannot work out is a problem to solve; a known route is a time to leave.
function travelProblems(travel) {
  if (travel?.unknown_reasons?.length) {
    return `<div class="banner warn">${icon('alert')}<div><strong>${esc(t('travel.unknown'))}</strong><p>${esc([...new Set(travel.unknown_reasons.map((r) => explainReason(r).text))].join(' · '))}</p></div></div>`;
  }
  if (travel?.infeasible_reasons?.length) {
    return `<div class="banner danger">${icon('alert')}<div><strong>${esc(t('travel.infeasible'))}</strong><p>${esc([...new Set(travel.infeasible_reasons.map((r) => explainReason(r).text))].join(' · '))}</p></div></div>`;
  }
  return '';
}

// Departures within this window are "leave soon" — an action, shown with the problems.
const LEAVE_SOON_MS = 2 * 3600000;

function travelTransitions(travel, cur = now()) {
  if (travel?.unknown_reasons?.length || travel?.infeasible_reasons?.length) return [];
  return (travel?.transitions || []).filter((x) => new Date(x.travel_ends_at) >= cur);
}

function travelCard(x) {
  return `
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
    </article>`;
}

const AUTH_FAILURE = /AUTH|CREDENTIAL|PERMISSION|TOKEN_EXPIRED|HTTP_401|HTTP_403/iu;

function sourceLine(source) {
  const actionRequired = AUTH_FAILURE.test(source.latest_failure_reason || '');
  const last = source.last_successful_complete_sync_at;
  return `<p>${esc(t(actionRequired ? 'today.sourceAction' : 'today.sourceTransient'))}${last ? ` ${esc(t('today.sourceLastKnown', { when: fmtDateTime(last) }))}` : ''}</p>${actionRequired ? `<button class="button small" data-nav="settings">${esc(t('today.sourceFix'))}</button>` : ''}`;
}

// What needs the user's decision before the plan can be trusted. Empty when nothing
// does: a plan that fits is not news and lives in the day details below.
// What the open quotas still ask for today, in the plan's terms: time it keeps for
// them, time that does not fit next to obligations, and quotas without a pace.
function quotaDemandLine(data) {
  const cap = data.day_capacity || {};
  if (!cap.quota_known_minutes && !cap.quota_unknown_count) return '';
  const unfit = (data.plan?.explanations || []).filter((r) => r.startsWith('QUOTA_DOES_NOT_FIT:'))
    .map((r) => (data.checkins || []).find((c) => c.template_id === r.split(':')[1])?.title).filter(Boolean);
  const parts = [];
  if (cap.quota_known_minutes) {
    parts.push(t(cap.quota_planned_minutes ? 'checkin.quotaPlanned' : 'checkin.quotaDemand', {
      known: fmtDuration(cap.quota_known_minutes), planned: fmtDuration(cap.quota_planned_minutes || 0),
      free: fmtDuration(cap.safe_reserve_after_quotas_minutes ?? 0) }));
  }
  if (unfit.length) parts.push(t('checkin.quotaUnfit', { titles: unfit.map((x) => `«${x}»`).join(', ') }));
  if (cap.quota_unknown_count) parts.push(t('checkin.quotaUnknown', { n: cap.quota_unknown_count }));
  return `<p class="help pad${unfit.length ? ' warn' : ''}" data-quota-demand>${esc(parts.join(' '))}</p>`;
}

export function attentionItems(data, cur = now()) {
  const plan = data.plan || {};
  const items = [];
  if (plan.feasibility_status && plan.feasibility_status !== 'FEASIBLE') items.push(heroStatus(plan, data.tasks || []));
  const travelProblem = travelProblems(data.travel);
  if (travelProblem) items.push(travelProblem);
  for (const x of travelTransitions(data.travel, cur)) {
    if (new Date(x.latest_safe_departure).getTime() - cur.getTime() <= LEAVE_SOON_MS) items.push(travelCard(x));
  }
  const blocked = (data.source_health || []).filter((s) => s.health_status !== 'CURRENT' && AUTH_FAILURE.test(s.latest_failure_reason || ''));
  if (blocked.length) items.push(`<div class="banner warn" data-source-action>${icon('alert')}<div>${sourceLine(blocked[0])}</div></div>`);
  return items;
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
    <p class="muted">${esc(t('today.block', { d: fmtDuration(action.recommended_duration_minutes) }))}${task?.effort_estimate_source === 'SYSTEM_PROVISIONAL' ? ` · ${esc(t('task.provisional', { lo: fmtDuration(task.remaining_effort_low_minutes ?? 15), hi: fmtDuration(task.remaining_effort_high_minutes ?? 60) }))}` : ''}${action.relevant_at ? ` · ${esc(t('today.due', { when: fmtDateTime(action.relevant_at) }))}` : ''}</p>
    ${task ? `<div class="now-actions">
      <button class="button primary" data-action="start-task" data-id="${esc(task.id)}">${esc(t('today.start'))}</button>
      <button class="button ghost" data-action="complete-task" data-id="${esc(task.id)}">${esc(t('lifecycle.complete'))}</button>
      <details><summary>${esc(t('capture.more'))}</summary>
      <button class="button ghost" data-action="progress" data-id="${esc(task.id)}" data-minutes="${esc(action.recommended_duration_minutes)}">${icon('check')}${esc(t('today.didBlock', { d: fmtDuration(action.recommended_duration_minutes) }))}</button>
      <button class="button ghost" data-action="defer-task" data-id="${esc(task.id)}">${esc(t('today.notNow'))}</button>
      <button class="button ghost" data-action="reschedule-task" data-id="${esc(task.id)}">${esc(t('task.reschedule'))}</button>
      </details>
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
    <p class="muted">${esc(item.why)}${task.effort_estimate_source === 'SYSTEM_PROVISIONAL' ? ` · ${esc(t('task.provisional', { lo: fmtDuration(task.remaining_effort_low_minutes ?? 15), hi: fmtDuration(task.remaining_effort_high_minutes ?? 60) }))}` : task.remaining_effort_minutes != null ? ` · ${esc(t('tasks.left', { d: fmtDuration(task.remaining_effort_minutes) }))}` : ''}</p>
    <div class="now-actions">
      ${task.started_at ? `<button class="button primary" data-action="progress" data-id="${esc(task.id)}">${icon('check')}${esc(t('task.logProgress'))}</button>`
        : `<button class="button primary" data-action="start-task" data-id="${esc(task.id)}">${esc(t('today.start'))}</button>`}
      <button class="button ghost" data-action="complete-task" data-id="${esc(task.id)}">${esc(t('lifecycle.complete'))}</button>
      <details><summary>${esc(t('capture.more'))}</summary>
      <button class="button ghost" data-action="defer-task" data-id="${esc(task.id)}">${esc(t('today.notNow'))}</button>
      <button class="button ghost" data-action="reschedule-task" data-id="${esc(task.id)}">${esc(t('task.reschedule'))}</button>
      </details>
    </div>
  </article>`;
}

function openDailyIntent(data) {
  const current = data.daily_intent || {};
  const selected = new Set(current.priority_task_ids?.length ? current.priority_task_ids : (data.next_actions || []).slice(0, 3).map((action) => action.task_id));
  const tasks = (data.tasks || []).filter((task) => task.status === 'ACTIVE');
  const rows = tasks.map((task) => `<label class="row">
    <input type="checkbox" data-intent-task value="${esc(task.id)}" ${selected.has(task.id) ? 'checked' : ''}>
    <span class="row-main"><strong>${esc(task.title)}</strong>
      <small>${task.remaining_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(t('tasks.left', { d: fmtDuration(task.remaining_effort_minutes) }))}</small></span>
    ${riskChip(task.risk)}
  </label>`).join('');
  const dialog = openSheet({
    title: t('intent.title'),
    body: `<p class="help">${esc(t('intent.help'))}</p>
      ${rows ? `<div class="list">${rows}</div>` : `<p class="muted">${esc(t('intent.noTasks'))}</p>`}
      <label class="field"><span>${esc(t('intent.note'))}</span>
        <textarea rows="3" maxlength="2000" data-intent-note>${esc(current.note || '')}</textarea>
      </label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-intent-save>${esc(t('common.save'))}</button>`,
  });
  const checks = [...dialog.querySelectorAll('[data-intent-task]')];
  checks.forEach((box) => box.addEventListener('change', () => {
    if (checks.filter((item) => item.checked).length > 3) box.checked = false;
  }));
  dialog.querySelector('[data-intent-save]').addEventListener('click', async (event) => {
    setBusy(event.currentTarget, true);
    const payload = {
      local_date: data.local_date,
      priority_task_ids: checks.filter((item) => item.checked).map((item) => item.value),
      note: String(dialog.querySelector('[data-intent-note]').value || '').trim() || null,
    };
    if (current.version) payload.expected_version = Number(current.version);
    const result = await change('intent.set', data.local_date, payload, { success: t('intent.saved') });
    if (result) dialog.close('saved'); else setBusy(event.currentTarget, false);
  });
}

function dailyIntentCard(data, tasks) {
  const intent = data.daily_intent;
  const ids = intent?.priority_task_ids || [];
  const chosen = ids.map((id) => tasks.get(id)).filter(Boolean);
  if (!intent || intent.closed_at) {
    return `<section class="section"><button class="link" data-action="intent-edit">${icon('flag')} ${esc(t('intent.choose'))}</button></section>`;
  }
  return `<section class="section"><article class="card">
    <div class="section-head"><div><span class="eyebrow">${esc(t('intent.eyebrow'))}</span>
      <h3>${esc(t('intent.today'))}</h3></div>
      ${intent._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
    </div>
    ${chosen.length ? `<ol class="intent-list">${chosen.map((task) => `<li><button class="link" data-action="open-task" data-id="${esc(task.id)}">${esc(task.title)}</button></li>`).join('')}</ol>`
      : `<p class="muted">${esc(t('intent.noPriorities'))}</p>`}
    ${intent.note ? `<p>${esc(intent.note)}</p>` : ''}
    <div class="now-actions">
      <button class="button ghost" data-action="intent-edit">${esc(t('intent.edit'))}</button>
      <button class="button ghost" data-nav="reflection">${esc(t('intent.review'))}</button>
      <button class="button ghost" data-action="intent-close">${esc(t('intent.close'))}</button>
    </div>
  </article></section>`;
}

// Standalone reminders due today (and ones that rang and still wait for an answer)
// that "Soon" does not already show.
function todayReminders(reminders = [], cur = now(), shown = new Set()) {
  const list = reminders.filter((r) => isOpen(r) && !shown.has(r.id) && (sameDay(new Date(r.remind_at), cur) || r.status === 'FIRED'))
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

function eventBits(e, cur) {
  const running = new Date(e.starts_at) <= cur;
  const bits = [running ? t('today.eventNow') : fmtRelative(e.starts_at)];
  if (!sameDay(new Date(e.starts_at), cur)) bits.push(fmtDateTime(e.starts_at));
  if (e.attendance_policy !== 'REQUIRED') bits.push(code('attendance', e.attendance_policy));
  if (e.location_effect?.kind && e.location_effect.kind !== 'NONE') bits.push(code('location', e.location_effect.kind));
  if (e.remind_before_minutes != null) bits.push(t('event.remindShort', { n: e.remind_before_minutes }));
  return bits.join(' · ');
}

function soonRow(x, cur) {
  if (x.kind === 'EVENT') {
    const e = x.event;
    return `<button class="row soon-row${x.running ? ' current' : ''}" data-soon-kind="EVENT" data-action="open-event" data-id="${esc(e.id)}">
      <span class="row-time"><strong>${esc(fmtTime(e.starts_at))}</strong><small>${esc(fmtTime(e.ends_at))}</small></span>
      <span class="row-main"><strong>${esc(e.title)}</strong><small>${esc(eventBits(e, cur))}</small></span>
      ${x.running ? chip(t('today.eventNowChip'), 'accent') : icon('event')}
    </button>`;
  }
  if (x.kind === 'REMINDER') {
    const r = x.reminder;
    return `<button class="row soon-row" data-soon-kind="REMINDER" data-action="open-reminder" data-kind="REMINDER" data-id="${esc(r.id)}">
      <span class="row-time"><strong>${esc(fmtTime(r.remind_at))}</strong><small>${icon(hasAlarm(r.delivery) ? 'clock' : 'bell')}</small></span>
      <span class="row-main"><strong>${esc(r.title)}</strong><small>${esc(fmtRelative(r.remind_at))}</small></span>
    </button>`;
  }
  return `<div class="row soon-row" data-soon-kind="TASK">
    <button class="row-main plain" data-action="open-task" data-id="${esc(x.task.id)}"><strong>${esc(x.task.title)}</strong><small>${esc(x.why)}</small></button>
    ${x.ready ? `<button class="button small ghost" data-action="start-task" data-id="${esc(x.task.id)}">${esc(t('today.start'))}</button>` : ''}
    <button class="button small ghost" data-action="complete-task" data-id="${esc(x.task.id)}" aria-label="${esc(t('lifecycle.complete'))}">${icon('check')}</button>
  </div>`;
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
    const activeExecution = data.active_execution || null;
    const activeExecutionTask = activeExecution ? tasks.get(activeExecution.task_id) : null;
    const unhealthy = (data.source_health || []).filter((s) => s.health_status !== 'CURRENT');
    const cur = now();
    const events = (data.events || plan.canonical_events || []).slice().sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at));
    // A running event takes "Now" unless a work session already does; then it leads "Soon".
    const currentEvent = activeExecution ? null : runningEvent(data.upcoming_events || events, cur);
    const soon = soonTasks(data, cur);
    const suggestion = !first && !currentEvent && !activeExecution ? soon.find((x) => x.ready) : null;
    const later = soon.filter((x) => x !== suggestion);
    const upcoming = upcomingItems({ events: data.upcoming_events || events, reminders: data.reminders || [], tasks: later }, cur,
      { nowEventId: currentEvent?.id });
    const inSoon = new Set(upcoming.filter((x) => x.kind !== 'TASK').map((x) => x.id));
    const otherEvents = events.filter((e) => e.id !== currentEvent?.id && !inSoon.has(e.id));
    const atRisk = (data.tasks || []).filter((x) => ['AT_RISK', 'CRITICAL', 'IMPOSSIBLE', 'OVERDUE'].includes(x.risk?.state));
    const attention = attentionItems(data, cur);
    const laterTravel = travelTransitions(data.travel, cur)
      .filter((x) => new Date(x.latest_safe_departure).getTime() - cur.getTime() > LEAVE_SOON_MS);
    const bounds = boundaries(data);
    const transientSources = unhealthy.filter((s) => !AUTH_FAILURE.test(s.latest_failure_reason || ''));
    const inbox = data.inbox_notes || [];

    const nothingYet = !(data.tasks || []).length && !(data.needs_refinement || []).length && !events.length && !(data.reminders || []).length && !(data.checkins || []).length;
    const capture = `<button class="capture-cta" data-action="compose">
        <span class="capture-cta-copy"><strong>${esc(t('capture.title'))}</strong><small>${esc(t('capture.ctaHint'))}</small></span>
        <span class="capture-cta-icons">${icon('plus')}</span></button>`;
    const feasible = plan.feasibility_status === 'FEASIBLE';
    // The summary line of the day details: the plan status when it fits (not news),
    // plus how much is folded inside, so nothing is hidden without a trace.
    const folded = [
      otherEvents.length ? t('today.foldEvents', { n: otherEvents.length }) : '',
      bounds.length ? t('today.foldBoundaries', { n: bounds.length }) : '',
      inbox.length ? t('today.foldCaptures', { n: inbox.length }) : '',
    ].filter(Boolean).join(' · ');
    return `
      ${nothingYet ? `<section class="section">${capture}<p class="help pad">${esc(t('today.firstHint'))}</p></section>` : ''}

      <section class="section ${nothingYet ? 'hidden' : ''}" data-now>
        ${sectionHead(t('today.now'))}
        ${activeExecution ? executionCard(activeExecution, activeExecutionTask) : currentEvent ? `<article class="card now-card event-now-card" data-action="open-event" data-id="${esc(currentEvent.id)}">
          <div class="now-head"><span class="eyebrow">${esc(t('today.eventNow'))}</span>${chip(t('today.fixedEvent'), 'accent')}</div>
          <h3>${esc(currentEvent.title)}</h3>
          <p class="muted">${esc(fmtTime(currentEvent.starts_at))}–${esc(fmtTime(currentEvent.ends_at))}</p>
        </article>` : first ? nowCard(first, tasks.get(first.task_id), plan) : suggestion ? fallbackNowCard(suggestion) : nothingYet ? '' : empty(
          feasible ? t('today.nothing') : t('today.resolveFirst'),
          feasible ? t('today.nothingHint') : t('today.resolveFirstHint'),
          feasible ? 'check' : 'question',
        )}
      </section>

      ${attention.length && !nothingYet ? `<section class="section attention" data-attention>
        ${sectionHead(t('today.attention'))}
        ${attention.join('')}
      </section>` : ''}

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

      ${todayCheckinsSection(data.checkins || [], { quotaBlocks: plan.quota_blocks || [] })}
      ${quotaDemandLine(data)}

      ${upcoming.length ? `<section class="section" data-soon>
        ${sectionHead(t('today.soon'))}
        <div class="list">${upcoming.map((x) => soonRow(x, cur)).join('')}</div>
      </section>` : ''}

      ${laterTravel.length ? `<section class="section">${sectionHead(t('today.travel'))}${laterTravel.map((x) => travelCard(x)).join('')}</section>` : ''}

      ${atRisk.length ? `<section class="section">
        ${sectionHead(t('today.atRisk'))}
        <div class="list">${atRisk.map((x) => `<button class="row" data-action="open-task" data-id="${esc(x.id)}">
          <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(t('tasks.left', { d: fmtDuration(x.remaining_effort_minutes) }))}${x.risk?.latest_safe_start ? ` · ${esc(t('tasks.startBy', { when: fmtDateTime(x.risk.latest_safe_start) }))}` : ''}</small></span>
          ${riskChip(x.risk)}
        </button>`).join('')}</div>
      </section>` : ''}

      ${todayReminders(data.reminders, cur, inSoon)}

      ${data.needs_refinement?.length ? `<section class="section">
        ${sectionHead(t('today.needsRefinement'))}
        <div class="list">${data.needs_refinement.map((x) => `<button class="row" data-action="open-task" data-id="${esc(x.id)}">
          <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(t('today.needsEstimate'))}</small></span>
          ${icon('chevron')}
        </button>`).join('')}</div>
      </section>` : ''}

      ${nothingYet ? '' : dailyIntentCard(data, tasks)}

      ${transientSources.length ? `<details class="section quiet" data-source-status><summary>${esc(t('today.sourceStatus'))}</summary>${transientSources.map(sourceLine).join('')}</details>` : ''}

      ${nothingYet ? '' : `<details class="section quiet" data-planner-status data-day-details>
        <summary>${feasible ? icon(statusIcon(plan.feasibility_status)) : ''}<span class="summary-copy">${esc(feasible ? t('status.title.FEASIBLE') : t('today.dayDetails'))}${folded ? ` <small class="muted">· ${esc(folded)}</small>` : ''}</span></summary>
        ${feasible ? heroStatus(plan, data.tasks || []) : ''}

        ${inbox.length ? `<section class="section">
          ${sectionHead(t('today.captures'), `<button class="link" data-nav="notes">${esc(t('nav.notes'))}</button>`)}
          <div class="list">${inbox.map((n) => `<button class="row" data-action="open-note" data-id="${esc(n.id)}">
            <span class="row-icon tone-accent">${icon(n.audio ? 'mic' : 'note')}</span>
            <span class="row-main"><strong>${esc((n.content || n.transcript || t('note.voice')).slice(0, 120))}</strong><small>${esc(t('note.unprocessed'))}</small></span>
            ${icon('chevron')}
          </button>`).join('')}</div>
        </section>` : ''}

        <section class="section">
          ${sectionHead(t('today.events'), `<button class="link" data-nav="calendar">${esc(t('nav.calendar'))}</button>`)}
          ${otherEvents.length ? `<div class="list">${otherEvents.map((e) => {
            const running = new Date(e.starts_at) <= cur;
            return `<button class="row${running ? ' current' : ''}" data-action="open-event" data-id="${esc(e.id)}">
            <span class="row-time"><strong>${esc(fmtTime(e.starts_at))}</strong><small>${esc(fmtTime(e.ends_at))}</small></span>
            <span class="row-main"><strong>${esc(e.title)}</strong><small>${esc(eventBits(e, cur))}</small></span>
            ${running ? chip(t('today.eventNowChip'), 'accent') : ''}
          </button>`;
          }).join('')}</div>` : `<p class="muted pad">${esc(t(events.length ? 'today.eventsAbove' : 'today.noEvents'))}</p>`}
        </section>

        ${bounds.length ? `<section class="section">
          ${sectionHead(t('today.boundaries'))}
          <div class="list">${bounds.map((b) => `<button class="row" data-action="open-task" data-id="${esc(b.id)}">
            <span class="row-icon tone-${b.kind === 'cutoff' ? 'danger' : b.kind === 'lss' ? 'warn' : 'accent'}">${icon(b.kind === 'cutoff' ? 'flag' : b.kind === 'lss' ? 'clock' : 'task')}</span>
            <span class="row-main"><strong>${esc(b.title)}</strong><small>${esc(t(`bound.${b.kind}`))} · ${esc(fmtDateTime(b.at))}</small></span>
            <span class="row-aside">${esc(fmtRelative(b.at))}</span>
          </button>`).join('')}</div>
        </section>` : ''}
      </details>`}`;
  },
  mount(root, data, ctx) {
    const session = data?.active_execution || null;
    const task = session ? (data?.tasks || []).find((x) => x.id === session.task_id) : null;
    mountExecutionTimers(root, session, task);
    const action = ctx.query?.execution_action;
    const requestedId = ctx.query?.session_id;
    if (!session || !action || requestedId !== session.id || action === 'open') return;
    const key = `${session.id}:${action}`;
    if (this._handledExecutionIntent === key) return;
    this._handledExecutionIntent = key;
    history.replaceState(null, '', '#/today');
    setTimeout(() => {
      if (action === 'pause') pauseExecution(session);
      else if (action === 'resume') resumeExecution(session);
      else if (action === 'finish' && task) finishExecution(session, task);
    }, 0);
  },
  actions: {
    'intent-edit'(_el, ctx) {
      openDailyIntent(ctx.data);
    },
    async 'intent-close'(_el, ctx) {
      if (!ctx.data?.local_date) return;
      const payload = { local_date: ctx.data.local_date };
      if (ctx.data.daily_intent?.version) payload.expected_version = Number(ctx.data.daily_intent.version);
      await change('intent.close', ctx.data.local_date, payload, { success: t('intent.closed') });
    },
    progress(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) logProgress(task, el.dataset.minutes);
    },
    async 'start-task'(el, ctx) {
      const task = (ctx.data?.tasks || []).find((x) => x.id === el.dataset.id);
      if (task) await startExecution(task, ctx.data?.plan);
    },
    async 'execution-pause'(_el, ctx) {
      if (ctx.data?.active_execution) await pauseExecution(ctx.data.active_execution);
    },
    async 'execution-resume'(_el, ctx) {
      if (ctx.data?.active_execution) await resumeExecution(ctx.data.active_execution);
    },
    async 'execution-finish'(_el, ctx) {
      const session = ctx.data?.active_execution;
      const task = session ? (ctx.data?.tasks || []).find((x) => x.id === session.task_id) : null;
      if (session && task) await finishExecution(session, task);
    },
    'execution-review'(_el, ctx) {
      const session = ctx.data?.active_execution;
      const task = session ? (ctx.data?.tasks || []).find((x) => x.id === session.task_id) : null;
      if (session && task) reviewLongExecution(session, task);
    },
    async 'execution-complete'(_el, ctx) {
      const session = ctx.data?.active_execution;
      const task = session ? (ctx.data?.tasks || []).find((x) => x.id === session.task_id) : null;
      if (session && task) await finishExecution(session, task, { complete: true });
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
