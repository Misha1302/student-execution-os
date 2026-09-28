// Today's "Soon": one time-ordered list of what comes next — tasks, fixed-time events
// and standalone reminders. Pure (no DOM, no i18n) so the rules are testable in Node:
// the view turns each item's `reason` into words.
//
// - A running event belongs to "Now", not "Soon" — unless "Now" is taken (an active
//   work session), in which case it stays here marked running.
// - Events and reminders come in within `hours` of now, across midnight: a class at
//   00:30 is "soon" at 23:00 even though it is tomorrow's.
// - Tasks keep the existing rules (soonTasks): open tasks the plan does not put first.

export const SOON_HOURS = 12;

const ms = (value) => new Date(value).getTime();

export function currentEvent(events, cur) {
  const t = cur.getTime();
  return (events || []).filter((e) => ms(e.starts_at) <= t && t < ms(e.ends_at))
    .sort((a, b) => ms(a.starts_at) - ms(b.starts_at))[0] || null;
}

// tasks: [{ task, why, ready }] from soonTasks; reminders: the account's reminders.
export function upcomingItems({ events = [], reminders = [], tasks = [] } = {}, cur, { hours = SOON_HOURS, nowEventId = null } = {}) {
  const t = cur.getTime();
  const horizon = t + hours * 3600000;
  const items = [];
  const seenEvents = new Set();
  for (const e of events) {
    if (!e || seenEvents.has(e.id) || e.id === nowEventId) continue;
    const start = ms(e.starts_at);
    const end = ms(e.ends_at);
    if (!(end > t) || start > horizon) continue;
    seenEvents.add(e.id);
    items.push({ kind: 'EVENT', id: e.id, at: e.starts_at, sort: start, running: start <= t, event: e });
  }
  for (const r of reminders) {
    if (!r || r.status !== 'SCHEDULED' || r.obligation_id) continue; // a task's reminder shows with its task
    const at = ms(r.remind_at);
    if (at <= t || at > horizon) continue;
    items.push({ kind: 'REMINDER', id: r.id, at: r.remind_at, sort: at, reminder: r });
  }
  for (const x of tasks) {
    const task = x.task;
    const timed = !x.ready && task.actionable_from ? task.actionable_from
      : task.actual_cutoff?.at || task.target_at || null;
    items.push({ kind: 'TASK', id: task.id, at: timed, sort: timed ? ms(timed) : Number.MAX_SAFE_INTEGER, ready: x.ready, why: x.why, task });
  }
  // Running first, then by time; untimed tasks last in their given order.
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => Number(b.item.running || false) - Number(a.item.running || false)
      || a.item.sort - b.item.sort || a.index - b.index)
    .map(({ item }) => item);
}
