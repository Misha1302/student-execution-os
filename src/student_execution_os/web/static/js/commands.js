// Commands about things the user already has, in everyday RU/EN phrasing — the
// device twin of agent/commands.py (same grammar, same fixture:
// tests/fixtures/nl_command_cases.json), so "готово эссе" or "перенеси созвон на
// 18:00" also work offline. A parse is only a proposal: it is previewed, and
// closing/archiving anything is confirmed before a sync operation is queued.
import { parseTask } from './nlparse.js';

const PUNCT = /["'«»“”„()!?.,;:]+/gu;
const LEAD_PREPS = /^(?:над|по|на|про|о|об|с|со|задачу|задача|событие|напоминание|on|for|about|of|at|the|task|event|reminder)\s+/iu;
const CLOCK_WORDS = /\d{1,2}[:.]\d{2}|(?<!\p{L})(?:в|во|к|at|by)\s+\d{1,2}(?!\d)|\d\s*(?:am|pm)|утр|вечер|дн[её]м|ноч|полдень|полночь|morning|evening|afternoon|night|noon|midnight|через|in\s+\d/iu;

const COMPLETE = /^(?:готово|сделано|сделал[аи]?|выполнил[аи]?|выполнено|закончил[аи]?|отметь(?:\s+как)?\s+выполненн\p{L}*|done|complete|completed|finished|mark\s+done)\s*[-—:]?\s+(?<target>.+)$/iu;
const COMPLETE_SUFFIX = /^(?<target>.+?)\s*[-—:]?\s+(?:готово|сделано|выполнено|done)$/iu;
const ARCHIVE = /^(?:в\s+архив|архивируй|заархивируй|убери\s+в\s+архив|archive)\s*[-—:]?\s+(?<target>.+)$/iu;
const ARCHIVE_SUFFIX = /^(?<target>.+?)\s+в\s+архив$/iu;
const CANCEL = /^(?:отмени(?:ть)?|не\s+буду\s+делать|cancel|drop)\s*[-—:]?\s+(?<target>.+)$/iu;
const MOVE = /^(?:перенеси(?:те)?|перенести|сдвинь|передвинь|move|reschedule|postpone)\s+(?<rest>.+)$/iu;
const SNOOZE = /^(?:напомни(?:те)?(?:\s+мне)?\s+(?:про|о|об)|remind\s+me\s+(?:about|of)|отложи|snooze)\s+(?<rest>.+)$/iu;
const PROGRESS = /^(?:поработал[аи]?|позанимал(?:ся|ась|ись)|занимал(?:ся|ась|ись)|потратил[аи]?|worked|spent|logged)\s+(?<rest>.+)$/iu;
const COUNT = /^(?:сделал[аи]?|решил[аи]?|прочитал[аи]?|написал[аи]?|did|solved|read|finished)\s+(?<n>\d{1,5})\s+(?<unit>\S+)\s+(?:по|из|в|для|for|of|in|from)\s+(?<target>.+)$/iu;

export function normalize(text) {
  return String(text || '').toLowerCase().replaceAll('ё', 'е').replace(PUNCT, ' ').split(/\s+/u).filter(Boolean).join(' ');
}

// A crude RU/EN stem: endings are vowels ("лабу"/"лаба", "матану"/"матан").
const stem = (word) => (word.replace(/[аеиоуыэюяйьeiouy]+$/u, '') || word).slice(0, 5);

function stems(text) {
  return new Set(normalize(text).split(' ').filter((w) => w.length > 2 || /^\d+$/u.test(w)).map(stem));
}

// How well a spoken fragment names an item (1.0 exact … 0 unrelated).
export function score(fragment, title) {
  const a = normalize(fragment);
  const b = normalize(title);
  if (!a || !b) return 0;
  if (a === b) return 1;
  if (a.length >= 3 && (b.includes(a) || a.includes(b))) return 0.9;
  const sa = stems(a);
  const sb = stems(b);
  if (!sa.size || !sb.size) return 0;
  if ([...sa].every((x) => sb.has(x))) return 0.85;
  const common = [...sa].filter((x) => sb.has(x)).length;
  return common / new Set([...sa, ...sb]).size;
}

// The single item the fragment names, or null plus the closest candidates.
export function matchTarget(fragment, items, { kinds = ['TASK', 'EVENT', 'REMINDER'], statuses = null } = {}) {
  const pool = (items || []).filter((x) => kinds.includes(x.kind) && (!statuses || statuses.includes(x.status)));
  const scored = pool.map((item, index) => ({ value: score(fragment, item.title), index, item }))
    .sort((a, b) => b.value - a.value || a.index - b.index);
  const candidates = scored.filter((x) => x.value >= 0.34).slice(0, 5).map((x) => x.item);
  if (!scored.length || scored[0].value < 0.5) return { item: null, candidates };
  if (scored.length > 1 && scored[1].value >= scored[0].value - 0.05) return { item: null, candidates };
  return { item: scored[0].item, candidates };
}

function cleanTarget(text) {
  let value = String(text).split(/\s+/u).filter(Boolean).join(' ').replace(/^[\s\-—:,.]+|[\s\-—:,.]+$/gu, '');
  for (;;) {
    const stripped = value.replace(LEAD_PREPS, '');
    if (stripped === value) return value;
    value = stripped;
  }
}

function momentOf(parsed) {
  for (const key of ['starts_at', 'remind_at', 'actionable_from', 'target_at']) if (parsed[key]) return new Date(parsed[key]);
  return parsed.actual_cutoff?.at ? new Date(parsed.actual_cutoff.at) : null;
}

function action(command, payload, { item, target, confirm = false, unresolved = [] }) {
  const missing = [...unresolved];
  let body = payload;
  if (!item) { body = { ...payload, target_text: target }; missing.unshift('target'); }
  else body = { [item.kind === 'REMINDER' ? 'reminder_id' : 'obligation_id']: item.id, ...payload };
  return { command, payload: body, confidence: item ? 0.9 : 0.5, unresolved_fields: missing,
    expected_version: item ? item.version ?? null : null, requires_confirmation: confirm };
}

// The sync operation that moves a task, event or reminder to `when`. `keepTime`:
// only a day was said, so the item keeps its own time of day.
export function rescheduleChange(kind, entity, when, keepTime) {
  let current;
  if (kind === 'EVENT') current = entity.starts_at;
  else if (kind === 'REMINDER') current = entity.remind_at;
  else current = entity.actual_cutoff?.state === 'KNOWN' ? entity.actual_cutoff.at : entity.actionable_from;
  let at = new Date(when);
  if (keepTime && current) {
    const old = new Date(current);
    at = new Date(at.getFullYear(), at.getMonth(), at.getDate(), old.getHours(), old.getMinutes());
  }
  const value = at.toISOString();
  if (kind === 'EVENT') return ['event.update', { starts_at: value }];
  if (kind === 'REMINDER') return ['reminder.update', { remind_at: value }];
  if (entity.actual_cutoff?.state === 'KNOWN') return ['task.update', { actual_cutoff: { state: 'KNOWN', at: value, boundary: entity.actual_cutoff.boundary || 'INCLUSIVE' } }];
  return ['task.defer', { until: value }];
}

// parseCommand(text, now, items) → one typed action, or null (read it as something new).
// items: [{ id, kind: TASK|EVENT|REMINDER, title, status, version }]
export function parseCommand(text, now = new Date(), items = []) {
  const raw = String(text || '').split(/\s+/u).filter(Boolean).join(' ').replace(/^[\s.!]+|[\s.!]+$/gu, '');
  if (!raw) return null;
  const open = ['ACTIVE', 'DRAFT', 'SCHEDULED', 'FIRED'];

  let m = COUNT.exec(raw);
  if (m) {
    const target = cleanTarget(m.groups.target);
    const { item } = matchTarget(target, items, { kinds: ['TASK'], statuses: ['ACTIVE', 'DRAFT'] });
    if (item || !items.length) return action('LOG_PROGRESS', { count: Number(m.groups.n) }, { item, target });
  }
  m = PROGRESS.exec(raw);
  if (m) {
    const parsed = parseTask(m.groups.rest, now);
    const minutes = parsed.estimated_total_effort_minutes;
    const target = cleanTarget(parsed.title || '');
    if (minutes && target) {
      const { item } = matchTarget(target, items, { kinds: ['TASK'], statuses: ['ACTIVE', 'DRAFT'] });
      return action('LOG_PROGRESS', { minutes: Number(minutes) }, { item, target });
    }
  }
  for (const [pattern, command, kinds, statuses, suffix] of [
    [COMPLETE, 'COMPLETE_OBLIGATION', ['TASK', 'REMINDER'], open, false],
    [ARCHIVE, 'ARCHIVE_OBLIGATION', ['TASK'], null, false],
    [CANCEL, 'CANCEL_OBLIGATION', ['TASK', 'EVENT', 'REMINDER'], open, false],
    [COMPLETE_SUFFIX, 'COMPLETE_OBLIGATION', ['TASK', 'REMINDER'], open, true],
    [ARCHIVE_SUFFIX, 'ARCHIVE_OBLIGATION', ['TASK'], null, true],
  ]) {
    const hit = pattern.exec(raw);
    if (!hit) continue;
    const target = cleanTarget(hit.groups.target);
    const { item } = matchTarget(target, items, { kinds, statuses });
    if (!item && suffix) continue; // only a known item makes a suffix a command
    return action(command, {}, { item, target, confirm: true });
  }
  m = MOVE.exec(raw);
  if (m) {
    const parsed = parseTask(m.groups.rest, now);
    const when = momentOf(parsed);
    const target = cleanTarget(parsed.title || m.groups.rest);
    const { item } = matchTarget(target, items, { statuses: open });
    const payload = when ? { when: when.toISOString(), keep_time: !CLOCK_WORDS.test(m.groups.rest) } : {};
    return action('RESCHEDULE', payload, { item, target, unresolved: when ? [] : ['when'] });
  }
  m = SNOOZE.exec(raw);
  if (m) {
    const parsed = parseTask(m.groups.rest, now);
    let until = momentOf(parsed);
    if (!until && parsed.estimated_total_effort_minutes) until = new Date(now.getTime() + parsed.estimated_total_effort_minutes * 60000);
    const target = cleanTarget(parsed.title || m.groups.rest);
    const { item } = matchTarget(target, items, { kinds: ['TASK', 'REMINDER'], statuses: open });
    if (!item || !until) return null; // "напомни про встречу с деканом завтра": a new reminder
    return action('SNOOZE', { until: until.toISOString() }, { item, target });
  }
  return null;
}
