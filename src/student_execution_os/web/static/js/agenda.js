// One list of everything the user has committed to — tasks, events and reminders —
// for the agenda and for search. The device twin of web/commitments.py (same
// fixture: tests/fixtures/commitments_cases.json). Task, Event and Reminder stay
// separate entities with their own operations; a commitment only presents one and
// keeps it under `entity`. Built from the cached lists (plus queued changes), so it
// works offline.

const TASK_PLACE = { ACTIVE: 'open', DRAFT: 'open', COMPLETED: 'done', CANCELLED: 'archive', ARCHIVED: 'archive' };
const REMINDER_PLACE = { SCHEDULED: 'open', FIRED: 'open', DONE: 'done', CANCELLED: 'archive' };

const fold = (text) => String(text || '').toLowerCase().replaceAll('ё', 'е').split(/\s+/u).filter(Boolean).join(' ');

export function commitmentOf(kind, entity, now = new Date()) {
  let at = null;
  let atKind = null;
  let endsAt = null;
  let place;
  let text;
  if (kind === 'TASK') {
    place = TASK_PLACE[entity.status] || 'open';
    const cutoff = entity.actual_cutoff || {};
    if (cutoff.state === 'KNOWN' && cutoff.at) { at = cutoff.at; atKind = 'due'; }
    else if (entity.target_at) { at = entity.target_at; atKind = 'target'; }
    else if (entity.actionable_from) { at = entity.actionable_from; atKind = 'from'; }
    text = [entity.title, entity.description].filter(Boolean).join(' ');
  } else if (kind === 'EVENT') {
    at = entity.starts_at || null; atKind = 'starts'; endsAt = entity.ends_at || null;
    const ended = endsAt != null && new Date(endsAt) <= now;
    place = entity.status !== 'ACTIVE' ? 'archive' : ended ? 'done' : 'open';
    text = [entity.title, entity.description].filter(Boolean).join(' ');
  } else {
    place = REMINDER_PLACE[entity.status] || 'open';
    at = entity.remind_at || null; atKind = 'remind';
    text = [entity.title, entity.note].filter(Boolean).join(' ');
  }
  return {
    kind, id: entity.id, title: entity.title || '', status: entity.status ?? null,
    place, at, at_kind: atKind, ends_at: endsAt,
    importance: entity.importance || 'NORMAL', version: entity.version ?? null,
    search_text: fold(text),
    entity,
  };
}

function order(items, place) {
  const timed = items.filter((x) => x.at);
  const untimed = items.filter((x) => !x.at);
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  timed.sort((a, b) => (new Date(a.at) - new Date(b.at)) || cmp(a.kind, b.kind) || cmp(a.id, b.id));
  if (place === 'done' || place === 'archive') timed.reverse();
  untimed.sort((a, b) => cmp(a.title.toLowerCase(), b.title.toLowerCase()) || cmp(a.id, b.id));
  return [...timed, ...untimed];
}

export function commitments({ tasks = [], events = [], reminders = [] } = {}, { now = new Date(), place = null, query = '' } = {}) {
  let items = [
    ...tasks.map((x) => commitmentOf('TASK', x, now)),
    ...events.map((x) => commitmentOf('EVENT', x, now)),
    ...reminders.map((x) => commitmentOf('REMINDER', x, now)),
  ];
  const words = fold(query).split(' ').filter(Boolean);
  if (words.length) items = items.filter((x) => words.every((w) => x.search_text.includes(w)));
  if (place) items = items.filter((x) => x.place === place);
  return order(items, place);
}

// Local calendar day of a commitment for grouping ("today", "tomorrow", a date, none).
export function dayOf(item) {
  if (!item.at) return null;
  const d = new Date(item.at);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
