// Device mirror of agent/recurring.py: «каждый день в 9 напоминай принять витамин D»,
// «по 20 задач в день», «по будням утром напомни взять пропуск». Held to the same
// fixture as the server (tests/fixtures/nl_recurring_cases.json). Only an explicit
// recurrence marker makes a phrase recurring; the medication kind comes from the verb
// the user used, never from a list of drug names.
import { PARTS, hourOf } from './nlparse.js';

const L = '(?<![0-9a-zа-я])';
const R = '(?![0-9a-zа-я])';
const WEEKDAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU'];
const DAY_STEMS = [['понедельник', 0], ['вторник', 1], ['сред', 2], ['четверг', 3], ['пятниц', 4], ['суббот', 5], ['воскресен', 6],
  ['monday', 0], ['tuesday', 1], ['wednesday', 2], ['thursday', 3], ['friday', 4], ['saturday', 5], ['sunday', 6]];
const DAY_WORD = '(?:понедельник[а-я]*|вторник[а-я]*|сред[а-я]*|четверг[а-я]*|пятниц[а-я]*|суббот[а-я]*|воскресен[а-я]*'
  + '|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)';
const MARKERS = [
  ['каждый\\s+будний\\s+день|по\\s+будн(?:ям|им\\s+дням)|в\\s+будни|on\\s+weekdays|every\\s+weekday|weekdays', 'WEEKDAYS', null],
  ['по\\s+выходным|каждые\\s+выходные|on\\s+weekends|every\\s+weekend', 'WEEKENDS', null],
  [`(?:кажд(?:ый|ую|ое)\\s+|по\\s+|every\\s+|on\\s+)${DAY_WORD}(?:\\s*(?:,|и|and)\\s*${DAY_WORD})*`, 'DAYS', null],
  ['каждое\\s+утро|every\\s+morning', 'DAILY', 'morning'],
  ['каждый\\s+вечер|every\\s+evening|every\\s+night', 'DAILY', 'evening'],
  ['каждый\\s+день|ежедневно|every\\s+day|daily', 'DAILY', null],
  ['через\\s+день|every\\s+other\\s+day', 'EVERY_OTHER_DAY', null],
  ['каждую\\s+неделю|раз\\s+в\\s+неделю|every\\s+week|weekly|once\\s+a\\s+week', 'WEEKLY', null],
  ['в\\s+день|per\\s+day|a\\s+day', 'PER_DAY', null],
];
const REMIND = `${L}(?:напоминай(?:те)?|напомни(?:те)?|напоминать|remind)(?:\\s+(?:мне|me))?(?:\\s+(?:to|о|об|про|что))?${R}|${L}не\\s+забы(?:ть|вать)${R}`;
const TIME = `${L}(?:в|во|at)\\s+(\\d{1,2})(?:[:.](\\d{2}))?\\s*(утра|вечера|дня|ночи|am|pm)?${R}(?!\\s*(?:мин|час|ч${R}|%))`;
const TIME_AND = '^\\s*(?:и|and)\\s+(?:в\\s+|at\\s+)?(\\d{1,2})(?:[:.](\\d{2}))?\\s*(утра|вечера|дня|ночи|am|pm)?(?![0-9a-zа-я])';
const PART_WORD = [
  [`${L}(?:утром|с\\s+утра|in\\s+the\\s+morning)${R}`, 'morning'],
  [`${L}(?:дн[её]м|in\\s+the\\s+afternoon)${R}`, 'afternoon'],
  [`${L}(?:вечером|in\\s+the\\s+evening)${R}`, 'evening'],
  [`${L}(?:перед\\s+сном|на\\s+ночь|before\\s+bed)${R}`, 'night'],
];
const MEDICATION_VERB = `${L}(?:принять|принимать|выпить|пить|take)${R}\\s+`;
const EN_MEDICATION = `${L}(?:pills?|tablets?|capsules?|drops|vitamins?|medicines?|medications?|meds)${R}`;
const NOT_MEDICATION = '^(?:душ|ванн|решени|участи|гост|экзамен|зач[её]т|звон|заказ|посылк|a\\s+shower|a\\s+bath|part|notes)';
const DOSE = `${L}\\d+(?:[.,]\\d+)?\\s*(?:мг|mg|мкг|mcg|мл|ml|ме|iu|ед)${R}`
  + `|${L}\\d+\\s*(?:таблетк[а-я]*|капсул[а-я]*|капел[а-я]*|капл[а-я]*|tablets?|pills?|drops?)(?:\\s+(?:of)(?![0-9a-zа-я]))?`;
// Groups: 1 = optional «по », 2 = count, 3 = spacing, 4 = unit (positions without the d flag).
const QUOTA = `${L}(по\\s+)?${L}(\\d{1,6})(\\s+)([a-zа-я]{2,30})`;
const SKIP_UNIT = '^(?:раз|минут[а-я]*|мин|час[а-я]*|ч|утра|вечера|дня|ночи|am|pm|minutes?|hours?|times?|мг|mg|мл|ml)$';
const TRACK = `${L}(?:отмечать|отслеживать|трекать|привычк[а-я]*|track|habit)${R}`;

const re = (pattern, flags = '') => new RegExp(pattern, flags);
const lowered = (text) => text.toLowerCase().replaceAll('ё', 'е');
const pad = (n) => String(n).padStart(2, '0');

function blank(chars, start, end) {
  for (let i = Math.max(0, start); i < Math.min(end, chars.length); i += 1) chars[i] = ' ';
}

function trimPunct(text) { return text.replace(/^[ ,.;:—\-!]+|[ ,.;:—\-!]+$/gu, ''); }

function clean(text) {
  let out = trimPunct(text.replace(/\s+/gu, ' '));
  for (;;) {
    const stripped = out.replace(/^(?:и|and|чтобы|to|of)\s+/iu, '').replace(/\s+(?:и|and|of)$/iu, '');
    if (stripped === out) break;
    out = trimPunct(stripped);
  }
  return trimPunct(out);
}

const cap = (text) => text.slice(0, 1).toUpperCase() + text.slice(1);

function firstStart(timeValue, ruleKind, days, now, includeToday = false) {
  const [hh, mm] = timeValue.split(':').map(Number);
  const allowed = { WEEKDAYS: [0, 1, 2, 3, 4], WEEKENDS: [5, 6], DAYS: days }[ruleKind];
  for (let offset = 0; offset < 15; offset += 1) {
    const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, hh, mm);
    if (candidate <= now && !(includeToday && offset === 0)) continue;
    if (allowed && !allowed.includes((candidate.getDay() + 6) % 7)) continue;
    return `${candidate.getFullYear()}-${pad(candidate.getMonth() + 1)}-${pad(candidate.getDate())}T${pad(candidate.getHours())}:${pad(candidate.getMinutes())}`;
  }
  return null;
}

function rule(ruleKind, days) {
  if (ruleKind === 'WEEKDAYS') return 'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR';
  if (ruleKind === 'WEEKENDS') return 'FREQ=WEEKLY;BYDAY=SA,SU';
  if (ruleKind === 'DAYS') return `FREQ=WEEKLY;BYDAY=${[...new Set(days)].sort((a, b) => a - b).map((d) => WEEKDAYS[d]).join(',')}`;
  if (ruleKind === 'EVERY_OTHER_DAY') return 'FREQ=DAILY;INTERVAL=2';
  if (ruleKind === 'WEEKLY') return 'FREQ=WEEKLY';
  return 'FREQ=DAILY';
}

const timeText = (hour, minute, suffix, explicit) => `${pad(hourOf(hour, suffix, explicit))}:${pad(minute)}`;

export function parseRecurring(text, now = new Date()) {
  const raw = String(text || '').split(/\s+/u).filter(Boolean).join(' ');
  if (!raw || raw.length > 300) return null;
  const low = lowered(raw);
  if (low.length !== raw.length) return null;
  let marker = null;
  for (const [pattern, ruleKind, part] of MARKERS) {
    const found = re(`${L}(?:${pattern})${R}`).exec(low);
    if (found) { marker = [found, ruleKind, part]; break; }
  }
  if (!marker) return null;
  const [found] = marker;
  let [, ruleKind, impliedPart] = marker;
  const timeSpans = [...low.matchAll(re(TIME, 'g'))].map((m) => [m.index, m.index + m[0].length]);
  const perDay = re(`${L}(?:в\\s+день|per\\s+day|a\\s+day)${R}`).exec(low);
  let quota = null;
  for (const m of low.matchAll(re(QUOTA, 'g'))) {
    const countAt = m.index + (m[1] || '').length;
    const unitAt = countAt + m[2].length + m[3].length;
    if (timeSpans.some(([a, b]) => a <= countAt && countAt < b) || re(SKIP_UNIT).test(m[4])) continue;
    if (re(REMIND).test(m[4])) continue;
    const end = m.index + m[0].length;
    if (m[0].startsWith('по') || (perDay && perDay.index >= end)) {
      quota = { start: m.index, countAt, unitAt, unitEnd: unitAt + m[4].length, count: Number(m[2]), byPo: Boolean(m[1]) };
      break;
    }
  }
  if (ruleKind === 'PER_DAY') {
    if (!quota) return null; // «в день» alone is no recurrence («в день рождения»)
    ruleKind = 'DAILY';
  }
  const days = [];
  const masked = [...raw];
  if (ruleKind === 'DAYS') {
    for (const word of found[0].match(re(DAY_WORD, 'g')) || []) {
      for (const [stem, index] of DAY_STEMS) if (word.startsWith(stem)) days.push(index);
    }
  } else if (ruleKind === 'WEEKLY') {
    const named = re(`${L}(?:в|во|on)\\s+(${DAY_WORD})${R}`).exec(low);
    if (named) {
      for (const [stem, index] of DAY_STEMS) if (named[1].startsWith(stem)) days.push(index);
      ruleKind = 'DAYS';
      blank(masked, named.index, named.index + named[0].length);
    }
  }
  blank(masked, found.index, found.index + found[0].length);
  if (perDay) blank(masked, perDay.index, perDay.index + perDay[0].length);
  let times = [];
  const firstTime = re(TIME).exec(low);
  if (firstTime) {
    const hour = Number(firstTime[1]);
    const minute = Number(firstTime[2] || 0);
    let suffix = firstTime[3] || null;
    if (hour <= 23 && minute <= 59) {
      if (!suffix && impliedPart === 'evening' && hour < 12) suffix = 'вечера';
      else if (!suffix && impliedPart === 'morning') suffix = 'утра';
      times.push(timeText(hour, minute, suffix, firstTime[2] != null || hour > 12));
      const end = firstTime.index + firstTime[0].length;
      blank(masked, firstTime.index, end);
      const extra = re(TIME_AND).exec(low.slice(end));
      if (extra && Number(extra[1]) <= 23 && Number(extra[2] || 0) <= 59) {
        const h2 = Number(extra[1]);
        times.push(timeText(h2, Number(extra[2] || 0), extra[3] || null, extra[2] != null || h2 > 12));
        blank(masked, end, end + extra[0].length);
      }
    }
  }
  let part = impliedPart;
  for (const [pattern, name] of PART_WORD) {
    const hit = re(pattern).exec(low);
    if (hit) { blank(masked, hit.index, hit.index + hit[0].length); part = part || name; }
  }
  if (!times.length && part) times.push(`${pad(PARTS[part][2][0])}:${pad(PARTS[part][2][1])}`);
  for (const hit of low.matchAll(re(REMIND, 'g'))) blank(masked, hit.index, hit.index + hit[0].length);
  times = [...new Set(times)].sort();
  const result = { recurrence_rule: rule(ruleKind, days), times, unresolved: [] };
  if (quota) {
    if (quota.byPo) blank(masked, quota.start, quota.countAt);
    const title = clean(masked.join(''));
    if (!title) return null;
    Object.assign(result, { kind: 'CHECKIN', checkin_kind: 'QUOTA', title: cap(title), target_quantity: quota.count,
      unit: raw.slice(quota.unitAt, quota.unitEnd), remind: Boolean(times.length) });
    result.dtstart_local = firstStart(times[0] || '00:00', ruleKind, days, now, !times.length);
    return result;
  }
  const rest = masked.join('');
  const restLow = lowered(rest);
  let verb = re(MEDICATION_VERB).exec(restLow);
  if (verb && restLow.slice(verb.index, verb.index + 4) === 'take') {
    const after = restLow.slice(verb.index + verb[0].length);
    if (!re(EN_MEDICATION).test(after) && !re(DOSE).test(after)) verb = null;
  }
  const track = re(TRACK).exec(restLow);
  if (verb && !re(NOT_MEDICATION).test(restLow.slice(verb.index + verb[0].length))) {
    const from = verb.index + verb[0].length;
    let nameRaw = rest.slice(from);
    const dose = re(DOSE).exec(restLow.slice(from));
    if (dose) {
      result.dose_text = clean(nameRaw.slice(dose.index, dose.index + dose[0].length));
      nameRaw = `${nameRaw.slice(0, dose.index)} ${nameRaw.slice(dose.index + dose[0].length)}`;
    }
    const name = clean(nameRaw);
    if (!name) return null;
    Object.assign(result, { kind: 'CHECKIN', checkin_kind: 'MEDICATION', title: cap(name) });
  } else if (track) {
    const title = clean(`${rest.slice(0, track.index)} ${rest.slice(track.index + track[0].length)}`);
    if (!title) return null;
    Object.assign(result, { kind: 'CHECKIN', checkin_kind: 'ROUTINE', title: cap(title) });
  } else {
    const title = clean(rest);
    if (!title) return null;
    Object.assign(result, { kind: 'REMINDER_SERIES', title: cap(title) });
  }
  if (!times.length) {
    result.unresolved = ['time'];
    result.dtstart_local = null;
  } else {
    result.dtstart_local = firstStart(times[0], ruleKind, days, now);
  }
  return result;
}

// CREATE_CHECKIN / CREATE_REMINDER_SERIES actions for a reading (mirror of
// agent/checkin_actions.recurring_actions): two times are two series.
export function recurringActions(parsed, timezoneName) {
  const times = parsed.times?.length ? parsed.times : [null];
  return times.map((clock, index) => {
    let start = parsed.dtstart_local;
    if (clock && start && index > 0) start = `${start.slice(0, 10)}T${clock}`;
    const checkin = parsed.kind === 'CHECKIN';
    const payload = checkin
      ? { kind: parsed.checkin_kind, title: parsed.title, recurrence_rule: parsed.recurrence_rule, timezone_name: timezoneName }
      : { title: parsed.title, recurrence_rule: parsed.recurrence_rule, timezone_name: timezoneName };
    if (checkin) for (const key of ['dose_text', 'target_quantity', 'unit', 'remind']) if (parsed[key] != null) payload[key] = parsed[key];
    if (start) payload.dtstart_local = start;
    return { command: checkin ? 'CREATE_CHECKIN' : 'CREATE_REMINDER_SERIES', payload, confidence: 0.85,
      unresolved_fields: start ? [] : ['dtstart_local'], expected_version: null, requires_confirmation: false };
  });
}

// «Я витамин уже принял», «вечерний приём пропущу» about a known check-in (mirror of
// agent/checkin_actions.parse_outcome); which day's dose is meant is decided later.
const DONE_WORDS = /(?<![0-9a-zа-я])(?:уже\s+)?(?:принял[аи]?|выпил[аи]?|сделал[аи]?|took|done with|have taken)(?![0-9a-zа-я])/u;
const SKIP_WORDS = /(?<![0-9a-zа-я])(?:пропущу|пропустил[аи]?|не\s+принял[аи]?|не\s+буду\s+принимать|не\s+выпил[аи]?|skip(?:ping)?|didn'?t\s+take|did\s+not\s+take)(?![0-9a-zа-я])/u;
const DAY_PART_WORDS = [[/утренн|утром|morning/u, 'MORNING'], [/дневн|днем|днём|afternoon/u, 'AFTERNOON'],
  [/вечерн|вечером|evening|tonight/u, 'EVENING'], [/ночн|на ночь|night/u, 'NIGHT']];
const STOP = new Set('с со на в во к ко по и о об у за до от для из под над при про мне мой моя моё мое мои меня мою моей мной это эту этот эта ту тот та его её ее их ей ему им ним ней нее неё него же ли бы а но или the a an to with at on in of my for and or by from'.split(' '));
const words = (text) => (lowered(String(text)).match(/[0-9a-zа-я]+/gu) || []).filter((w) => !STOP.has(w) && w.length > 1);
function sameWord(a, b) {
  if (a === b) return true;
  if (/^\d+$/.test(a) || /^\d+$/.test(b)) return false;
  const shortest = Math.min(a.length, b.length);
  if (shortest < 3) return false;
  let common = 0;
  while (common < shortest && a[common] === b[common]) common += 1;
  return common >= Math.max(3, shortest - 2);
}
function score(title, said) {
  const own = words(title);
  if (!own.length) return [0, 0];
  const matched = own.filter((w) => said.some((s) => sameWord(w, s))).length;
  return [matched / own.length, matched];
}

export function parseCheckinOutcome(text, checkins = []) {
  const low = lowered(String(text || '')).split(/\s+/u).filter(Boolean).join(' ');
  const skip = SKIP_WORDS.test(low);
  const done = !skip && DONE_WORDS.test(low);
  if (!(skip || done) || !checkins.length) return null;
  const said = words(low);
  const scored = checkins.map((item) => [score(item.title, said), item]).sort((a, b) => b[0][0] - a[0][0] || b[0][1] - a[0][1]);
  const best = scored[0][0];
  let fitting = best[0] >= 0.5 ? scored.filter(([s]) => s[0] === best[0] && s[1] === best[1]).map(([, item]) => item) : [];
  const generic = said.some((w) => ['прием', 'лекарство', 'таблетку', 'таблетки', 'dose', 'pill'].some((x) => sameWord(w, x)));
  if (!fitting.length && generic) fitting = checkins.filter((item) => item.checkin_kind === 'MEDICATION');
  if (!fitting.length) return null;
  const payload = { outcome: skip ? 'SKIPPED' : 'DONE', target_text: String(text).trim().slice(0, 300) };
  for (const [pattern, part] of DAY_PART_WORDS) if (pattern.test(low)) { payload.day_part = part; break; }
  if (fitting.length === 1) payload.checkin_id = fitting[0].id;
  return { command: 'CHECKIN_OUTCOME', payload, confidence: 0.8,
    unresolved_fields: fitting.length === 1 ? [] : ['target', 'expected_version'],
    expected_version: fitting.length === 1 ? Number(fitting[0].version || 1) : null, requires_confirmation: false };
}
