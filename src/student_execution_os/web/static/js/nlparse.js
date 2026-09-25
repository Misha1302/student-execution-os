// Deterministic RU/EN natural-language task capture, running on the device.
//
// This is a rule-for-rule port of src/student_execution_os/agent/nlparse.py so that a
// simple task can be captured with no network and no language model. Both
// implementations are checked against tests/fixtures/nl_capture_cases.json.
//
// Dates are computed in the device's local time zone (the server-side parser uses the
// account's zone). The result has the shape of a task.create payload plus
// `unresolved` (what the UI should ask about) and `cutoff_time_assumed`.

// JavaScript's \w and \b are ASCII-only; Cyrillic needs explicit Unicode classes.
const W = '[\\p{L}\\p{N}_]';
function rx(source, flags = '') {
  const src = source
    .replaceAll('\\b', `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`)
    .replaceAll('[\\w', '[\\p{L}\\p{N}_')
    .replaceAll('\\w', W);
  return new RegExp(src, `gu${flags}`);
}

const NUM_WORDS = {
  ноль: 0, один: 1, одна: 1, одну: 1, одного: 1, одной: 1,
  два: 2, две: 2, двух: 2, три: 3, трех: 3, четыре: 4, четырех: 4,
  пять: 5, пяти: 5, шесть: 6, шести: 6, семь: 7, семи: 7,
  восемь: 8, восьми: 8, девять: 9, девяти: 9, десять: 10, десяти: 10,
  одиннадцать: 11, одиннадцати: 11, двенадцать: 12, двенадцати: 12,
  пятнадцать: 15, пятнадцати: 15, двадцать: 20, двадцати: 20,
  тридцать: 30, тридцати: 30, сорок: 40, сорока: 40, пятьдесят: 50,
  полтора: 1.5, полторы: 1.5, пару: 2, пары: 2, несколько: 3,
  zero: 0, one: 1, a: 1, an: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  fifteen: 15, twenty: 20, thirty: 30, forty: 40, fifty: 50,
  couple: 2, few: 3, several: 3,
};
const byLength = (list) => [...list].sort((a, b) => b.length - a.length);
const NUM_ALT = byLength(Object.keys(NUM_WORDS)).join('|');
const NUM = `(?:\\d+(?:[.,]\\d+)?|${NUM_ALT})`;
const H_UNIT = '(?:часик\\w*|час(?:а|ов)?|ч\\.?|hours?|hrs?|h)';
const M_UNIT = '(?:минут\\w*|мин\\.?|minutes?|mins?|m)';

const WEEKDAYS = {
  понедельник: 0, понедельника: 0, пн: 0, monday: 0, mon: 0,
  вторник: 1, вторника: 1, вт: 1, tuesday: 1, tue: 1, tues: 1,
  среда: 2, среду: 2, среды: 2, ср: 2, wednesday: 2, wed: 2,
  четверг: 3, четверга: 3, чт: 3, thursday: 3, thu: 3, thurs: 3,
  пятница: 4, пятницу: 4, пятницы: 4, пт: 4, friday: 4, fri: 4,
  суббота: 5, субботу: 5, субботы: 5, сб: 5, saturday: 5, sat: 5,
  воскресенье: 6, воскресенья: 6, вс: 6, sunday: 6, sun: 6,
  понедельнику: 0, вторнику: 1, среде: 2, четвергу: 3, пятнице: 4, субботе: 5, воскресенью: 6,
};
const WD_ALT = byLength(Object.keys(WEEKDAYS)).join('|');

const MONTHS = {
  январ: 1, феврал: 2, март: 3, апрел: 4, ма: 5, июн: 6, июл: 7, август: 8,
  сентябр: 9, октябр: 10, ноябр: 11, декабр: 12,
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
};
const RU_MONTH = '(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])';
const EN_MONTH = '(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)';

// [window start, window end, reminder anchor] as [h, m]
const PARTS = {
  morning: [[8, 0], [12, 0], [9, 0]],
  noon: [[12, 0], [12, 0], [12, 0]],
  afternoon: [[12, 0], [17, 0], [14, 0]],
  evening: [[18, 0], [22, 0], [19, 0]],
  night: [[21, 0], [23, 59], [22, 0]],
  midnight: [[23, 59], [23, 59], [23, 59]],
};
const DEADLINE_PART = { morning: [9, 0], noon: [12, 0], afternoon: [15, 0], evening: [18, 0], night: [23, 0], midnight: [23, 59] };

const PART_WORDS = [
  ['с\\s+утра|утром|утра|in\\s+the\\s+morning|this\\s+morning|morning', 'morning'],
  ['в\\s+полдень|к\\s+полудню|до\\s+полудня|полдень|at\\s+noon|by\\s+noon|noon|midday', 'noon'],
  ['к\\s+обеду|до\\s+обеда|после\\s+обеда|днем|днём|in\\s+the\\s+afternoon|afternoon', 'afternoon'],
  ['к\\s+вечеру|до\\s+вечера|вечером|вечера|сегодня\\s+вечером|tonight|in\\s+the\\s+evening|evening', 'evening'],
  ['ночью|на\\s+ночь|at\\s+night|night', 'night'],
  ['в\\s+полночь|до\\s+полуночи|к\\s+полуночи|at\\s+midnight|by\\s+midnight|midnight', 'midnight'],
];

const CATEGORY_RULES = [
  ['EXAM', 'экзамен|зач[её]т|контрольн|коллоквиум|сесси[яию]|\\bexam|midterm|\\bfinals?\\b|\\bquiz|\\btest\\b'],
  ['HOMEWORK', 'домашк|домашн|\\bдз\\b|д/з|лаб(?:а|у|ы|ораторн)|реферат|курсов|курсач|эссе|доклад|презентаци|слайд|конспект|задачи\\s+по|'
    + 'homework|assignment|\\blab\\b|essay|problem\\s+set|pset|coursework|\\bthesis'],
  ['ADMIN', 'справк|документ|заявлени|оплат|деканат|стипенди|паспорт|виз[аыу]\\b|анкет|'
    + 'paperwork|documents?|\\bpay\\b|\\bbill|application|\\bform\\b|\\bvisa\\b|register'],
  ['PERSONAL_APPOINTMENT', 'врач|стоматолог|парикмахер|записаться|больниц|поликлиник|doctor|dentist|appointment|haircut|clinic'],
  ['MEETING', 'встреч|созвон|собрани|\\bmeeting|\\bcall\\b|meet\\s+with'],
  ['ERRAND', 'купить|магазин|забрать|отнести|почт[аеуы]|аптек|\\bbuy\\b|pick\\s+up|groceries|\\bshop'],
  ['WORK', 'работ[аеуы]|отч[её]т|проект|клиент|\\breport|\\bwork\\b|project|client'],
  ['LESSON', '\\bпар[аеуы]\\b|лекци|семинар|занятие|урок|lecture|\\bclass\\b|seminar|lesson'],
];

const FILLERS = new Set([
  'надо', 'нужно', 'необходимо', 'мне', 'я', 'пожалуйста', 'плиз', 'это', 'а', 'и', 'но', 'ещё', 'еще',
  'please', 'i', 'need', 'to', 'have', 'must', 'should', 'gotta', 'it', 'and', 'also',
  'напомни', 'напомнить', 'напоминание', 'remind', 'me', 'reminder',
]);
const NOT_CLOCK = new Set(['a', 'an', 'couple', 'few', 'several', 'пару', 'пары', 'несколько', 'полтора', 'полторы', 'ноль', 'zero']);
const COUNTED = 'задач|этап|раз|страниц|глав|упражнен|пункт|част|шаг|урок|steps?|pages?|chapters?|tasks?|parts?|times?';
const EDGE_PREPS = new Set(['в', 'во', 'к', 'ко', 'до', 'на', 'по', 'с', 'со', 'за', 'at', 'by', 'on', 'in', 'for', 'until', 'before', 'from']);
const EXAM_DATIVE = {
  экзамен: 'экзамену', зачет: 'зачёту', зачёт: 'зачёту', контрольная: 'контрольной',
  контрольную: 'контрольной', коллоквиум: 'коллоквиуму', тест: 'тесту', сессия: 'сессии', сессию: 'сессии',
};

// ---- local calendar helpers (device time zone) ------------------------------------

const day = (y, m, d) => ({ y, m, d });
const toDay = (date) => day(date.getFullYear(), date.getMonth() + 1, date.getDate());
const addDays = (v, n) => toDay(new Date(v.y, v.m - 1, v.d + n));
const weekday = (v) => (new Date(v.y, v.m - 1, v.d).getDay() + 6) % 7;
const dayValue = (v) => v.y * 10000 + v.m * 100 + v.d;
const at = (v, [h, mi]) => new Date(v.y, v.m - 1, v.d, h, mi, 0, 0);
const floorMinute = (date) => new Date(Math.floor(date.getTime() / 60000) * 60000);

function validDay(y, m, d) {
  const date = new Date(y, m - 1, d);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d;
}

function addMonths(v, months) {
  const index = v.m - 1 + months;
  const y = v.y + Math.floor(index / 12);
  const m = ((index % 12) + 12) % 12 + 1;
  for (const d of [v.d, 30, 29, 28]) if (validDay(y, m, d)) return day(y, m, d);
  return day(y, m, 28);
}

function num(token) {
  const key = String(token).trim().toLowerCase().replace(',', '.');
  if (key in NUM_WORDS) return NUM_WORDS[key];
  if (!/^\d+(?:\.\d+)?$/.test(key)) return null;
  return Number(key);
}

export function normalize(text) {
  let out = '';
  for (const char of String(text)) {
    let lower = char.toLowerCase();
    if (lower.length !== char.length) lower = char;
    out += lower === 'ё' ? 'е' : lower;
  }
  return out;
}

function cueOf(preposition) {
  if (!preposition) return null;
  const p = preposition.split(/\s+/).join(' ');
  if (['к', 'до', 'by', 'before', 'until', 'не позже', 'не позднее'].includes(p)) return 'deadline';
  if (['после', 'after', 'from', 'с', 'со', 'не раньше'].includes(p)) return 'start';
  return null;
}

function hourOf(hour, suffix, explicit) {
  const s = String(suffix || '').replaceAll('.', '');
  if (['pm', 'вечера', 'дня'].includes(s) && hour < 12) return hour + 12;
  if (s === 'ночи') return hour >= 9 && hour <= 11 ? hour + 12 : (hour === 12 ? 0 : hour);
  if (s === 'am' || s === 'утра') return hour === 12 ? 0 : hour;
  if (explicit && s === '') return hour;
  return hour >= 1 && hour <= 7 ? hour + 12 : hour;
}

function monthOf(token) {
  if (!token) return null;
  const low = token.toLowerCase();
  for (const prefix of byLength(Object.keys(MONTHS))) if (low.startsWith(prefix)) return MONTHS[prefix];
  return null;
}

// ---- span-tracking parser -----------------------------------------------------------

class Parser {
  constructor(text, now) {
    this.raw = text;
    this.low = normalize(text);
    this.now = now;
    this.today = toDay(now);
    this.taken = new Array(text.length).fill(false);
    this.pieces = [];
  }

  free(start, end) { for (let i = start; i < end; i += 1) if (this.taken[i]) return false; return true; }

  take(start, end) { for (let i = start; i < end; i += 1) this.taken[i] = true; }

  * scan(source) {
    for (const match of this.low.matchAll(rx(source))) {
      const start = match.index;
      const end = start + match[0].length;
      if (end > start && this.free(start, end)) yield { m: match, start, end };
    }
  }

  push(piece) { this.pieces.push({ dateOnlyRelative: false, sameWeekday: false, cue: null, ...piece }); }

  importance() {
    const rules = [
      ['LOW', '(?:это\\s+)?(?:не\\s+срочно|не\\s+важно|неважно|не\\s+горит|когда-нибудь|когда\\s+будет\\s+время|'
        + 'not\\s+urgent|low\\s+priority|no\\s+rush|not\\s+important|whenever|someday)'],
      ['CRITICAL', '(?:это\\s+)?(?:очень|супер|крайне)\\s+(?:важно|срочно)|критично|горит|'
        + '(?:very|super|extremely)\\s+(?:important|urgent)|critical'],
      ['HIGH', "(?:это\\s+)?(?:важн(?:о|ая|ый|ое)|срочн(?:о|ая|ый|ое))|(?:it'?s\\s+)?(?:important|urgent|asap|high\\s+priority)"],
    ];
    let found = null;
    for (const [level, pattern] of rules) {
      for (const hit of this.scan(`(?<![\\w-])(?:${pattern})(?![\\w-])\\s*!*`)) {
        this.take(hit.start, hit.end);
        found = found || level;
      }
    }
    if (found == null) {
      const bangs = /!{2,}\s*$/u.exec(this.low);
      if (bangs) { this.take(bangs.index, bangs.index + bangs[0].length); found = 'HIGH'; }
    }
    return found;
  }

  noDeadline() {
    let hit = false;
    for (const x of this.scan('(?<!\\w)(?:без\\s+(?:дедлайна|срока)|no\\s+deadline|without\\s+(?:a\\s+)?deadline)(?!\\w)')) {
      this.take(x.start, x.end);
      hit = true;
    }
    return hit;
  }

  relative() {
    const pattern = `(?<!\\w)(?:(к|до|by|within)\\s+)?(?:через|in)\\s+(?:(${NUM})\\s+)?`
      + '(минут\\w*|мин|час\\w*|дн(?:я|ей)|день|сутки|недел\\w*|месяц\\w*|minutes?|mins?|hours?|days?|weeks?|months?)(?!\\w)';
    for (const x of this.scan(pattern)) {
      const amount = x.m[2] ? num(x.m[2]) : 1;
      if (amount == null) continue;
      const unit = x.m[3];
      const cue = x.m[1] ? 'deadline' : null;
      let value; let kind = 'date'; let dateOnly = true;
      if (unit.startsWith('мин') || unit.startsWith('min')) {
        value = new Date(this.now.getTime() + amount * 60000); kind = 'instant'; dateOnly = false;
      } else if (unit.startsWith('час') || unit.startsWith('hour')) {
        value = new Date(this.now.getTime() + Math.round(amount * 60) * 60000); kind = 'instant'; dateOnly = false;
      } else if (unit.startsWith('д') || unit.startsWith('сут') || unit.startsWith('day')) {
        value = addDays(this.today, Math.round(amount));
      } else if (unit.startsWith('нед') || unit.startsWith('week')) {
        value = addDays(this.today, Math.round(amount * 7));
      } else {
        value = addMonths(this.today, Math.round(amount));
      }
      this.take(x.start, x.end);
      this.push({ start: x.start, end: x.end, kind, value, cue, dateOnlyRelative: dateOnly });
    }
  }

  periodEnds() {
    const rules = [
      ['(?:к|до)\\s+конца\\s+(?:этой\\s+)?недели|by\\s+the\\s+end\\s+of\\s+(?:the|this)\\s+week|by\\s+end\\s+of\\s+week', 'week_end', 'deadline'],
      ['(?:к|до)\\s+конца\\s+(?:этого\\s+)?месяца|by\\s+the\\s+end\\s+of\\s+(?:the|this)\\s+month', 'month_end', 'deadline'],
      ['на\\s+этой\\s+неделе|this\\s+week', 'week_end', 'target'],
      ['на\\s+следующей\\s+неделе|next\\s+week', 'next_week_end', 'target'],
    ];
    for (const [pattern, what, cue] of rules) {
      for (const x of this.scan(`(?<!\\w)(?:${pattern})(?!\\w)`)) {
        const t = this.today;
        let value;
        if (what === 'week_end') value = addDays(t, 6 - weekday(t));
        else if (what === 'next_week_end') value = addDays(t, 13 - weekday(t));
        else value = addDays(addMonths(day(t.y, t.m, 1), 1), -1);
        this.take(x.start, x.end);
        this.push({ start: x.start, end: x.end, kind: 'date', value, cue, dateOnlyRelative: true });
      }
    }
  }

  dates() {
    const t = this.today;
    const prep = '(?:(к|до|в|во|на|by|on|before|until|from|after|с|со|после)\\s+)?';
    for (const x of this.scan(`(?<!\\w)${prep}(сегодня|завтра|послезавтра|today|tomorrow|day\\s+after\\s+tomorrow)(?!\\w)`)) {
      const word = x.m[2];
      const offset = ['сегодня', 'today'].includes(word) ? 0 : (word === 'послезавтра' || /^day\s+after/.test(word) ? 2 : 1);
      this.datePiece(x, addDays(t, offset), x.m[1]);
    }
    for (const x of this.scan(`(?<!\\w)${prep}(?:(следующ\\w*|ближайш\\w*|этот|эту|это|next|this)\\s+)?(${WD_ALT})(?!\\w)`)) {
      const target = WEEKDAYS[x.m[3]];
      let days = ((target - weekday(t)) % 7 + 7) % 7;
      const modifier = x.m[2] || '';
      if (modifier.startsWith('следующ') || modifier.startsWith('next')) days = days ? days + 7 : 7;
      this.datePiece(x, addDays(t, days), x.m[1], days === 0);
    }
    for (const x of this.scan(`(?<!\\w)${prep}(\\d{1,2})\\s+${RU_MONTH}(?:\\s+(\\d{4}))?(?:\\s*г(?:ода|\\.)?)?(?!\\w)`)) {
      this.calendarPiece(x, Number(x.m[2]), monthOf(x.m[3]), x.m[4], x.m[1]);
    }
    for (const x of this.scan(`(?<!\\w)${prep}${EN_MONTH}\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?(?:,?\\s+(\\d{4}))?(?!\\w)`)) {
      this.calendarPiece(x, Number(x.m[3]), monthOf(x.m[2]), x.m[4], x.m[1]);
    }
    for (const x of this.scan(`(?<!\\w)${prep}(\\d{1,2})(?:st|nd|rd|th)?\\s+(?:of\\s+)?${EN_MONTH}(?:,?\\s+(\\d{4}))?(?!\\w)`)) {
      this.calendarPiece(x, Number(x.m[2]), monthOf(x.m[3]), x.m[4], x.m[1]);
    }
    for (const x of this.scan(`(?<![\\w.:])${prep}(\\d{4})-(\\d{2})-(\\d{2})(?![\\w:])`)) {
      this.calendarPiece(x, Number(x.m[4]), Number(x.m[3]), x.m[2], x.m[1]);
    }
    for (const x of this.scan(`(?<![\\w.:])${prep}(\\d{1,2})[./](\\d{1,2})(?:[./](\\d{2,4}))?(?![\\w:]|\\.\\d)`)) {
      const d = Number(x.m[2]); const m = Number(x.m[3]);
      if (m >= 1 && m <= 12 && d >= 1 && d <= 31) this.calendarPiece(x, d, m, x.m[4], x.m[1]);
    }
  }

  datePiece(x, value, preposition, sameWeekday = false) {
    this.take(x.start, x.end);
    this.push({ start: x.start, end: x.end, kind: 'date', value, cue: cueOf(preposition), sameWeekday });
  }

  calendarPiece(x, d, m, year, preposition) {
    if (m == null) return;
    const t = this.today;
    let value;
    if (year) {
      const y = Number(year) + (year.length === 2 ? 2000 : 0);
      if (!validDay(y, m, d)) return;
      value = day(y, m, d);
    } else {
      if (!validDay(t.y, m, d)) return;
      value = day(t.y, m, d);
      if (dayValue(value) < dayValue(t)) {
        if (!validDay(t.y + 1, m, d)) return;
        value = day(t.y + 1, m, d);
      }
    }
    this.take(x.start, x.end);
    this.push({ start: x.start, end: x.end, kind: 'date', value, cue: cueOf(preposition) });
  }

  times() {
    const prep = '(?:(к|до|в|во|около|после|с|со|не\\s+позже|не\\s+позднее|не\\s+раньше|at|by|before|until|after|from|around)\\s+)?';
    const suffix = '(?:\\s*(утра|дня|вечера|ночи|am|pm|a\\.m\\.|p\\.m\\.))?';
    for (const x of this.scan(`(?<![\\w.:])${prep}(\\d{1,2})[:.](\\d{2})(?![\\w.:])${suffix}`)) {
      const h = Number(x.m[2]); const mi = Number(x.m[3]);
      if (h < 24 && mi < 60) this.timePiece(x, hourOf(h, x.m[4], true), mi, x.m[1]);
    }
    for (const x of this.scan(`(?<![\\w.:])${prep}(\\d{1,2})\\s*(am|pm|a\\.m\\.|p\\.m\\.)(?!\\w)`)) {
      this.timePiece(x, hourOf(Number(x.m[2]), x.m[3], true), 0, x.m[1]);
    }
    const words = `(\\d{1,2}|${NUM_ALT}|часу)`;
    const pattern = `(?<!\\w)(к|до|в|во|около|после|с|со|не\\s+позже|не\\s+позднее|at|by|before|until|after|around)\\s+${words}`
      + `(?:\\s*(?:час(?:а|ов|ам|у)?|ч\\.?|o'?clock))?${suffix}(?!\\s*(?:минут|мин|hours?|mins?|minutes?|дн|недел|week|day|${COUNTED}))(?!\\w)`;
    for (const x of this.scan(pattern)) {
      const token = x.m[2];
      if (NOT_CLOCK.has(token)) continue;
      let hour;
      if (token === 'часу') hour = 13;
      else {
        const n = num(token);
        if (n == null || n !== Math.trunc(n) || n < 0 || n > 23) continue;
        hour = hourOf(n, x.m[3], Boolean(x.m[3]));
      }
      this.timePiece(x, hour, 0, x.m[1]);
    }
    for (const [pattern, part] of PART_WORDS) {
      for (const x of this.scan(`(?<!\\w)(?:(к|до|by|before|until|after|после)\\s+)?(?:${pattern})(?!\\w)`)) {
        let cue = cueOf(x.m[1]);
        if (cue == null && /^(?:к|до)\s/u.test(x.m[0])) cue = 'deadline';
        if (cue == null && /^после\s/u.test(x.m[0])) cue = 'start';
        this.take(x.start, x.end);
        this.push({ start: x.start, end: x.end, kind: 'part', value: part, cue });
      }
    }
  }

  timePiece(x, hour, minute, preposition) {
    this.take(x.start, x.end);
    this.push({ start: x.start, end: x.end, kind: 'time', value: [hour % 24, minute], cue: cueOf(preposition) });
  }

  effort() {
    const prefix = '(?:(?:это\\s+)?(?:займ[её]т|займут|потребует(?:ся)?|уйд[её]т|нужно|надо|нужен|нужна)\\s+|'
      + '(?:it\\s+)?(?:will\\s+)?(?:takes?|need(?:s)?)\\s+|на\\s+|for\\s+)?'
      + '(?:(?:примерно|около|где-то|где\\s+то|приблизительно|about|around|roughly|approx\\.?|~)\\s*)?';
    const tail = '(?:\\s+работы|\\s+of\\s+work)?';
    let found = null;
    const record = (x, minutes) => {
      this.take(x.start, x.end);
      if (found == null && minutes > 0) found = Math.round(minutes);
    };
    for (const x of this.scan(`(?<!\\w)${prefix}(\\d+)\\s*${H_UNIT}\\s*(?:и\\s+)?(\\d+)\\s*${M_UNIT}(?!\\w)${tail}`)) {
      record(x, Number(x.m[1]) * 60 + Number(x.m[2]));
    }
    for (const x of this.scan(`(?<!\\w)${prefix}(?:полтора\\s+часа|час\\s+с\\s+половиной|(?:an?\\s+)?hour\\s+and\\s+a\\s+half|1\\.5\\s*(?:h|hours?|ч))(?!\\w)${tail}`)) record(x, 90);
    for (const x of this.scan(`(?<!\\w)${prefix}(?:полчаса|пол\\s+часа|half\\s+an\\s+hour|half\\s+hour)(?!\\w)${tail}`)) record(x, 30);
    for (const x of this.scan(`(?<!\\w)${prefix}(${NUM})\\s*(?:-?\\s*)(часик\\w*|час(?:а|ов)?|ч\\.?|hours?|hrs?|h)(?!\\w)${tail}`)) {
      const v = num(x.m[1]);
      if (v != null && v <= 24) record(x, v * 60);
    }
    for (const x of this.scan(`(?<!\\w)${prefix}(${NUM})\\s*(?:-?\\s*)(минут\\w*|мин\\.?|minutes?|mins?|m)(?!\\w)${tail}`)) {
      const v = num(x.m[1]);
      if (v != null && v <= 600 && !(x.m[2] === 'm' && !/^\d/.test(x.m[1]))) record(x, v);
    }
    for (const x of this.scan(`(?<!\\w)${prefix}(час(?:а|ов)?|минут)\\s+(${NUM_ALT}|\\d+)(?!\\w)${tail}`)) {
      const v = num(x.m[2]);
      if (v != null) record(x, x.m[1].startsWith('час') ? v * 60 : v);
    }
    const cuePrefix = '(?:(?:это\\s+)?(?:займ[её]т|потребует(?:ся)?|уйд[её]т|нужен|нужно|надо)\\s+(?:примерно\\s+|около\\s+)?|на\\s+|(?:takes?|for|need)\\s+)';
    for (const x of this.scan(`(?<!\\w)${cuePrefix}(?:(?:целый|один)\\s+)?(час|часик|an\\s+hour|one\\s+hour|a\\s+minute)(?!\\w)`)) {
      record(x, x.m[1].includes('час') || x.m[1].includes('hour') ? 60 : 1);
    }
    for (const x of this.scan('(?<!\\w)(?:весь\\s+день|целый\\s+день|all\\s+day)(?!\\w)')) record(x, 360);
    return found;
  }

  chunking() {
    const whole = [...this.scan('(?<!\\w)(?:за\\s+один\\s+раз|одним\\s+куском|за\\s+раз|in\\s+one\\s+(?:go|sitting))(?!\\w)')];
    whole.forEach((x) => this.take(x.start, x.end));
    if (whole.length) return [false, null];
    const parts = [...this.scan(`(?<!\\w)(?:по\\s+частям|частями|in\\s+(?:chunks|parts))(?:\\s*(?:по\\s+)?(${NUM})\\s*${M_UNIT})?(?!\\w)`)];
    parts.push(...this.scan(`(?<!\\w)(?:по|in\\s+(?:blocks|chunks)\\s+of)\\s+(\\d+)\\s*${M_UNIT}(?!\\w)`));
    let size = null;
    for (const x of parts) {
      this.take(x.start, x.end);
      if (x.m[1]) size = Math.trunc(num(x.m[1]) || 0) || null;
    }
    return parts.length ? [true, size] : [null, null];
  }

  reminderCue() {
    const spans = [];
    for (const x of this.scan('(?<!\\w)(?:напомни(?:те)?(?:\\s+мне)?|напомнить(?:\\s+мне)?|поставь\\s+напоминание|напоминание|remind\\s+me(?:\\s+to)?|reminder)(?!\\w)')) {
      this.take(x.start, x.end);
      spans.push([x.start, x.end]);
    }
    return spans;
  }

  deadlineWords() {
    return rx('(?<!\\w)(?:сдать|сдача|сдаю|дедлайн\\w*|срок\\w*|отправить\\s+до|due|deadline|submit|hand\\s+in|turn\\s+in)(?!\\w)').test(this.low);
  }

  stripDeadlineLabels() {
    for (const x of this.scan('(?<!\\w)(?:дедлайн|срок(?:\\s+сдачи)?|deadline|due(?:\\s+date)?)\\s*[:\\-—]?\\s*(?=\\S)')) this.take(x.start, x.end);
  }

  startWords() {
    for (const x of this.scan('(?<!\\w)(?:начать|начну|не\\s+раньше|starting|start|not\\s+before)(?=\\s+(?:с|со|в|во|on|at|from|$))')) {
      this.take(x.start, x.end);
      this.push({ start: x.start, end: x.end, kind: 'cue', value: null, cue: 'start' });
    }
  }
}

function momentCue(moment) {
  const cues = moment.pieces.map((p) => p.cue).filter(Boolean);
  for (const wanted of ['remind', 'deadline', 'start', 'target']) if (cues.includes(wanted)) return wanted;
  return null;
}
const momentStart = (moment) => Math.min(...moment.pieces.map((p) => p.start));
const momentGet = (moment, kind) => moment.pieces.find((p) => p.kind === kind) || null;

function group(pieces, low) {
  const ordered = pieces.filter((p) => p.kind !== 'cue').sort((a, b) => a.start - b.start);
  const moments = [];
  for (const piece of ordered) {
    const last = moments[moments.length - 1];
    if (last) {
      const gap = low.slice(Math.max(...last.pieces.map((p) => p.end)), piece.start);
      const kinds = new Set(last.pieces.map((p) => p.kind));
      const compatible = !(kinds.has(piece.kind) || piece.kind === 'instant' || kinds.has('instant')
        || (['time', 'part'].includes(piece.kind) && (kinds.has('time') || kinds.has('part'))));
      if (compatible && /^[\s,]*(?:(?:и|в|во|к|до|на|at|on|by)\s*)?[\s,]*$/u.test(gap)) { last.pieces.push(piece); continue; }
    }
    moments.push({ pieces: [piece] });
  }
  for (const cue of pieces.filter((p) => p.kind === 'cue')) {
    const following = moments.filter((m) => momentStart(m) >= cue.end);
    if (following.length) following[0].pieces.push({ ...cue });
  }
  return moments;
}

function resolve(moment, role, now, category, effort) {
  const instant = momentGet(moment, 'instant');
  const dayPiece = momentGet(moment, 'date');
  const clock = momentGet(moment, 'time');
  const part = momentGet(moment, 'part');
  const plus = (date, minutes) => new Date(date.getTime() + minutes * 60000);
  if (instant) {
    const value = floorMinute(instant.value);
    const key = { deadline: 'actual_cutoff', remind: 'remind_at', start: 'actionable_from', target: 'target_at' }[role];
    if (key) return [key, value, false];
    return ['window', [value, effort ? plus(value, effort) : null], false];
  }
  const today = toDay(now);
  const d = dayPiece ? dayPiece.value : null;
  const dateOnly = !clock && !part;
  const next = (hm) => {
    let candidate = at(d || today, hm);
    if (!d && candidate <= now) candidate = at(addDays(today, 1), hm);
    return candidate;
  };
  if (role === 'deadline') {
    let value;
    if (clock) value = next(clock.value);
    else if (part) value = next(DEADLINE_PART[part.value]);
    else value = at(d, category === 'EXAM' ? [9, 0] : [23, 59]);
    if (dayPiece && dayPiece.sameWeekday && value <= now) value = at(addDays(toDay(value), 7), [value.getHours(), value.getMinutes()]);
    return ['actual_cutoff', value, dateOnly];
  }
  if (role === 'remind') {
    let value;
    if (clock) value = next(clock.value);
    else if (part) value = next(PARTS[part.value][2]);
    else {
      value = at(d, [9, 0]);
      if (value <= now) value = plus(floorMinute(now), 5);
    }
    return ['remind_at', value, dateOnly];
  }
  if (role === 'start' || role === 'target') {
    const key = role === 'start' ? 'actionable_from' : 'target_at';
    let value;
    if (clock) value = next(clock.value);
    else if (part) value = next(PARTS[part.value][role === 'start' ? 0 : 1]);
    else value = at(d, role === 'start' ? [0, 0] : [23, 59]);
    return [key, value, dateOnly];
  }
  if (clock) {
    const start = next(clock.value);
    return ['window', [start, effort ? plus(start, effort) : null], false];
  }
  if (part) {
    const [startHm, endHm] = PARTS[part.value];
    const start = next(startHm);
    const finish = at(toDay(start), endHm);
    return ['window', [start, finish > start ? finish : null], false];
  }
  if (!d) return null;
  let start = at(d, [0, 0]);
  if (start < now) start = floorMinute(now);
  return ['window', [start, at(d, [23, 59])], true];
}

const STRIP = ',.;:!?—-–()"«»';
function stripChars(text, chars) {
  let a = 0; let b = text.length;
  while (a < b && chars.includes(text[a])) a += 1;
  while (b > a && chars.includes(text[b - 1])) b -= 1;
  return text.slice(a, b);
}

function examTitle(title) {
  const low = normalize(title);
  let m = /^(экзамен|зачет|контрольная|контрольную|коллоквиум|тест|сессия|сессию)((?:\s+по\s+[^,]+?)?)\s*,?\s*(подготовиться|готовиться)$/du.exec(low);
  if (m) {
    const noun = EXAM_DATIVE[m[1]] || m[1];
    const [s2, e2] = m.indices[2];
    return `${m[3]} к ${noun}${title.slice(s2, e2)}`.trim();
  }
  m = /^(exam|test|quiz|midterm|final)((?:\s+in\s+[^,]+?)?)\s*,?\s*(?:to\s+)?(prepare|study)$/u.exec(low);
  if (m) return `${m[3]} for ${m[1]}${m[2]}`.trim();
  return title;
}

function titleOf(parser, category) {
  const kept = parser.raw.split('').map((char, i) => (parser.taken[i] ? ' ' : char)).join('');
  let words = kept.split(/\s+/u).filter(Boolean);
  const bare = (word) => normalize(stripChars(word, STRIP));
  const drop = (word) => FILLERS.has(bare(word)) || EDGE_PREPS.has(bare(word)) || !bare(word);
  let changed = true;
  while (words.length && changed) {
    changed = false;
    while (words.length && drop(words[0])) { words.shift(); changed = true; }
    while (words.length && drop(words[words.length - 1])) { words.pop(); changed = true; }
  }
  words = words.filter((w, i) => !(['надо', 'нужно', 'мне', 'это', 'please'].includes(bare(w)) && i > 0));
  let title = words.join(' ');
  title = title.replace(/\s+([,.;:!?])/gu, '$1').replace(/([,;:])(?:\s*[,;:])+/gu, '$1');
  title = stripChars(title, ' ,.;:!?—-–');
  if (category === 'EXAM') title = examTitle(title);
  if (title) title = title[0].toUpperCase() + title.slice(1);
  return title.slice(0, 300);
}

const iso = (date) => (date ? date.toISOString() : null);

// parseTask(text, now = new Date()) → task.create-shaped proposal.
export function parseTask(text, now = new Date()) {
  const lines = String(text || '').trim().split(/\r?\n/u).map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return { title: '', unresolved: ['title'] };
  let head = lines[0].split(/\s+/u).join(' ');
  head = head.replace(/^(?:задача|задачу|task|todo|to-do)\s*:\s*/iu, '');
  const description = lines.slice(1).join('\n') || null;
  const parser = new Parser(head, now);

  const importance = parser.importance();
  const absent = parser.noDeadline();
  const remindSpans = parser.reminderCue();
  parser.stripDeadlineLabels();
  parser.startWords();
  parser.relative();
  parser.periodEnds();
  parser.dates();
  parser.times();
  let [splittable, chunk] = parser.chunking();
  const effort = parser.effort();

  let category = 'GENERAL';
  for (const [name, pattern] of CATEGORY_RULES) {
    if (rx(pattern).test(parser.low)) { category = name; break; }
  }

  const fields = {};
  const moments = group(parser.pieces, parser.low);
  const hasDeadlineWords = parser.deadlineWords();
  let cutoffDateOnly = false;
  for (const moment of moments) {
    let role = momentCue(moment);
    const start = momentStart(moment);
    if (role == null && remindSpans.length && remindSpans.some(([, end]) => end <= start && start - end <= 3)) role = 'remind';
    if (role == null && remindSpans.length && !('remind_at' in fields) && !hasDeadlineWords) role = 'remind';
    if (role == null && (hasDeadlineWords || category === 'EXAM') && !('actual_cutoff' in fields)) role = 'deadline';
    if (role == null && !('actual_cutoff' in fields) && moment.pieces.every((p) => p.kind === 'date' && p.dateOnlyRelative)) role = 'deadline';
    role = role || 'when';
    const resolved = resolve(moment, role, now, category, effort);
    if (!resolved) continue;
    const [key, value, dateOnly] = resolved;
    if (key === 'window') {
      const [s, finish] = value;
      if (!('actionable_from' in fields)) fields.actionable_from = s;
      if (finish && !('target_at' in fields)) fields.target_at = finish;
    } else if (!(key in fields)) {
      fields[key] = value;
      if (key === 'actual_cutoff') cutoffDateOnly = dateOnly;
    }
  }

  const title = titleOf(parser, category);
  const unresolved = [];
  if (!title) unresolved.push('title');
  if (effort == null) unresolved.push('estimated_total_effort_minutes');
  let cutoff;
  if (fields.actual_cutoff) cutoff = { state: 'KNOWN', at: iso(fields.actual_cutoff), boundary: 'INCLUSIVE' };
  else if (absent) cutoff = { state: 'ABSENT' };
  else if (fields.remind_at || fields.actionable_from || fields.target_at) cutoff = { state: 'ABSENT' };
  else { cutoff = { state: 'UNKNOWN' }; unresolved.push('actual_cutoff'); }
  if (splittable == null) splittable = Boolean(effort && effort >= 120 && ['EXAM', 'HOMEWORK', 'WORK', 'GENERAL'].includes(category));
  const payload = {
    title,
    description,
    category,
    importance: importance || 'NORMAL',
    estimated_total_effort_minutes: effort,
    actual_cutoff: cutoff,
    target_at: iso(fields.target_at),
    actionable_from: iso(fields.actionable_from),
    remind_at: iso(fields.remind_at),
    splittable: Boolean(splittable),
    min_chunk_minutes: null,
    max_chunk_minutes: null,
  };
  if (payload.splittable) {
    payload.min_chunk_minutes = Math.min(30, effort || 30);
    payload.max_chunk_minutes = chunk || Math.max(payload.min_chunk_minutes, Math.min(90, effort || 90));
  }
  payload.unresolved = unresolved;
  payload.cutoff_time_assumed = cutoffDateOnly;
  return payload;
}
