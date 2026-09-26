// «Дела»: everything the user has — tasks, events and reminders — in one agenda,
// grouped by day, with one search. Built on the device from the cached lists plus
// queued changes (agenda.js), so it works offline. Task, Event and Reminder stay
// separate: each row opens its own screen and its quick actions are its own.
import { load } from '../store.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative, fmtTime, fmtDay, now, dayKey } from '../i18n.js';
import { esc, icon, chip, riskChip, empty, chipGroup } from '../ui.js';
import { commitments, dayOf } from '../agenda.js';
import { reminderStatusChip, hasAlarm, reminderSheet, syncDeviceAlarms } from '../reminders.js';
import { eventSheet } from '../events.js';
import { SHARED_PATH, sharedRow, criticalityChip } from '../groups.js';

const SHARED_KINDS = new Set(['SHARED_EVENT', 'SHARED_OBLIGATION', 'ANNOUNCEMENT']);

const RISKY = new Set(['START_SOON', 'AT_RISK', 'CRITICAL', 'IMPOSSIBLE', 'OVERDUE']);
const PLACES = ['open', 'done', 'archive'];
const KINDS = ['ALL', 'TASK', 'EVENT', 'REMINDER', 'GROUP'];

let place = 'open';
let kind = 'ALL';
let query = '';

export function deadlineOf(task) {
  if (task.actual_cutoff?.state === 'KNOWN' && task.actual_cutoff.at) return { at: task.actual_cutoff.at, kind: 'cutoff' };
  if (task.target_at) return { at: task.target_at, kind: 'target' };
  return null;
}

// Progress as a share: counted items when the task has them ("3 из 10"), time otherwise.
export function progressOf(x) {
  const count = x.count_progress;
  if (count?.total) return { pct: Math.round((count.done / count.total) * 100), label: `${count.done}/${count.total}${count.unit ? ` ${count.unit}` : ''}` };
  const total = Number(x.estimated_total_effort_minutes || 0);
  const left = Number(x.remaining_effort_minutes || 0);
  const pct = total ? Math.round(((total - left) / total) * 100) : 0;
  return { pct, label: pct ? `${pct}%` : '' };
}

export function taskCard(x, { swipe = false } = {}) {
  const d = deadlineOf(x);
  const left = Number(x.remaining_effort_minutes || 0);
  const { pct, label } = progressOf(x);
  const overdue = d && new Date(d.at) < now() && x.status === 'ACTIVE';
  const archived = x.status === 'CANCELLED' || x.status === 'ARCHIVED';
  return `<button class="task-card agenda-row" data-action="open-task" data-kind="TASK" data-id="${esc(x.id)}" ${swipe ? 'data-swipe="archive"' : ''}>
    <span class="task-top">
      <span class="kind-icon kind-task">${icon('task')}</span>
      <strong class="task-title">${esc(x.title)}</strong>
      ${x.status === 'ACTIVE' ? riskChip(x.risk) : chip(archived ? t('place.archive') : code('status', x.status), x.status === 'COMPLETED' ? 'ok' : x.status === 'DRAFT' ? 'warn' : 'muted')}
    </span>
    <span class="task-meta">
      ${d ? `<span class="${overdue ? 'text-danger' : ''}">${icon(d.kind === 'cutoff' ? 'flag' : 'clock')}${esc(fmtDateTime(d.at))} · ${esc(fmtRelative(d.at))}</span>` : `<span class="muted">${icon('flag')}${esc(code('cutoffState', x.actual_cutoff?.state))}</span>`}
      <span>${x.status === 'COMPLETED' && x.completed_at ? esc(t('tasks.doneAt', { when: fmtDateTime(x.completed_at) })) : x.estimated_total_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(t('tasks.left', { d: fmtDuration(left) }))}</span>
    </span>
    <span class="progress-row"><span class="progress" aria-hidden="true"><span data-w="${pct}"></span></span>${label ? `<small>${esc(label)}</small>` : ''}${x._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}</span>
  </button>`;
}

function eventRow(e, item) {
  const cancelled = e.status !== 'ACTIVE';
  const span = `${fmtTime(e.starts_at)}–${fmtTime(e.ends_at)}`;
  return `<button class="task-card agenda-row event-row" data-action="open-event" data-kind="EVENT" data-id="${esc(e.id)}">
    <span class="task-top"><span class="kind-icon kind-event">${icon('event')}</span><strong class="task-title">${esc(e.title)}</strong>
      ${cancelled ? chip(t('place.archive'), 'muted') : item.place === 'done' ? chip(t('agenda.eventPast'), 'muted') : chip(t('agenda.event'), 'accent')}</span>
    <span class="task-meta"><span>${icon('calendar')}${esc(fmtDay(e.starts_at))}, ${esc(span)}</span>
      ${e.remind_before_minutes != null ? `<span>${icon('bell')}${esc(t('event.remindShort', { n: e.remind_before_minutes }))}</span>` : ''}
      ${e._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}</span>
    ${(item.annotations || []).map((a) => `<span class="annotation" data-action="open-shared" data-kind="SHARED_EVENT" data-id="${esc(a.id)}">
      ${icon('flag')}<strong>${esc(a.title)}</strong> · ${esc(code('eventKind', a.event_kind))} · ${esc(a.group_name)} ${criticalityChip(a.criticality)}</span>`).join('')}
  </button>`;
}

function reminderRow(r) {
  return `<button class="task-card agenda-row reminder-row" data-action="open-reminder" data-kind="REMINDER" data-id="${esc(r.id)}">
    <span class="task-top"><span class="kind-icon kind-reminder">${icon('bell')}</span><strong class="task-title">${esc(r.title)}</strong>${reminderStatusChip(r)}</span>
    <span class="task-meta"><span>${icon(hasAlarm(r.delivery) ? 'clock' : 'bell')}${esc(fmtDateTime(r.remind_at))} · ${esc(fmtRelative(r.remind_at))}</span>
      ${r.note ? `<span class="muted">${esc(r.note)}</span>` : ''}
      ${r._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}</span>
  </button>`;
}

export function commitmentRow(item) {
  if (SHARED_KINDS.has(item.kind)) return sharedRow(item.entity);
  if (item.kind === 'EVENT') return eventRow(item.entity, item);
  if (item.kind === 'REMINDER') return reminderRow(item.entity);
  // A finished task can be swiped into the archive.
  return taskCard(item.entity, { swipe: item.place === 'done' && item.entity.status === 'COMPLETED' });
}

// Open items grouped: overdue, today, tomorrow, later days, without a date.
function groups(items) {
  const cur = now();
  const today = dayKey(cur);
  const tomorrow = dayKey(new Date(cur.getTime() + 86400000));
  const out = new Map();
  const put = (key, label, item) => {
    if (!out.has(key)) out.set(key, { label, items: [] });
    out.get(key).items.push(item);
  };
  for (const item of items) {
    const day = dayOf(item);
    if (!day) { put('~none', t('agenda.noDate'), item); continue; }
    const overdue = new Date(item.at) < cur && (item.at_kind === 'due' || item.at_kind === 'remind') && day < today;
    if (overdue) put('!overdue', t('agenda.overdue'), item);
    else if (day === today) put(today, t('day.today'), item);
    else if (day === tomorrow) put(tomorrow, t('day.tomorrow'), item);
    else put(day, fmtDay(`${day}T12:00:00`), item);
  }
  return [...out.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([, g]) => g);
}

export default {
  id: 'tasks',
  tab: 'tasks',
  title: () => t('nav.tasks'),
  async load({ fresh }) {
    const [tasks, events, reminders, shared] = await Promise.all([
      load('/api/v1/tasks', { fresh }),
      load('/api/v1/events', { fresh }).catch(() => ({ data: [] })),
      load('/api/v1/reminders', { fresh }).catch(() => ({ data: [] })),
      load(SHARED_PATH, { fresh }).catch(() => ({ data: { items: [] } })),
    ]);
    syncDeviceAlarms();
    return { data: { tasks: tasks.data || [], events: events.data || [], reminders: reminders.data || [], shared: shared.data?.items || [] },
      stale: tasks.stale || events.stale || reminders.stale, fetchedAt: tasks.fetchedAt };
  },
  render(data) {
    this._data = data;
    const all = commitments(data, { now: now() });
    const byKind = (x) => kind === 'ALL' || x.kind === kind || (kind === 'GROUP' && SHARED_KINDS.has(x.kind));
    const counts = Object.fromEntries(PLACES.map((p) => [p, all.filter((x) => x.place === p && byKind(x)).length]));
    const list = commitments(data, { now: now(), place, query }).filter(byKind);
    const atRisk = place === 'open' ? list.filter((x) => x.kind === 'TASK' && RISKY.has(x.entity.risk?.state)).length : 0;
    const segments = PLACES.map((p) => [p, `${t(`agenda.place.${p}`)} ${counts[p] || ''}`.trim()]);
    let body;
    if (!all.length) {
      body = empty(t('tasks.emptyAll'), t('tasks.emptyAllHint'), 'tasks') + `<button class="button primary wide" data-action="compose-task">${icon('plus')}${esc(t('capture.title'))}</button>`;
    } else if (!list.length) {
      body = empty(query ? t('tasks.noMatch') : t(`agenda.empty.${place}`), '', place === 'open' ? 'check' : 'tasks');
    } else if (place === 'open' && !query) {
      body = groups(list).map((g) => `<section class="agenda-group"><h3 class="group-head">${esc(g.label)}</h3>
        <div class="task-list">${g.items.map(commitmentRow).join('')}</div></section>`).join('');
    } else {
      body = `<div class="task-list">${list.map(commitmentRow).join('')}</div>`;
    }
    return `
      <div class="toolbar">
        <label class="search">${icon('search')}<input type="search" data-task-search placeholder="${esc(t('agenda.search'))}" value="${esc(query)}" enterkeyhint="search"></label>
        <div class="segmented">${chipGroup('task-filter', segments, place)}</div>
        <div class="segmented kinds">${chipGroup('kind-filter', KINDS.map((k) => [k, t(`agenda.kind.${k}`)]), kind)}</div>
      </div>
      ${atRisk ? `<p class="help pad">${icon('alert')} ${esc(t('agenda.atRisk', { n: atRisk }))}</p>` : ''}
      ${place === 'done' && list.some((x) => x.kind === 'TASK') ? `<p class="help pad">${esc(t('agenda.swipeHint'))}</p>` : ''}
      <section class="section">${body}</section>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name === 'task-filter') { place = e.detail.value; ctx.rerender(); }
      if (e.detail.name === 'kind-filter') { kind = e.detail.value; ctx.rerender(); }
    });
    const input = root.querySelector('[data-task-search]');
    input?.addEventListener('input', () => {
      query = input.value;
      const pos = input.selectionStart;
      ctx.rerender().then(() => {
        const next = document.querySelector('[data-task-search]');
        next?.focus();
        next?.setSelectionRange(pos, pos);
      });
    });
  },
  actions: {
    'open-reminder'(el, ctx) {
      const r = (ctx.view._data?.reminders || []).find((x) => x.id === el.dataset.id);
      if (r) reminderSheet(r);
    },
    'open-event'(el, ctx) {
      const e = (ctx.view._data?.events || []).find((x) => x.id === el.dataset.id);
      if (e) eventSheet(e);
    },
  },
};
