import ru from './i18n/ru.js';
import en from './i18n/en.js';

const DICTS = { ru, en };
export const LOCALES = [['ru', 'Русский'], ['en', 'English']];
const KEY = 'seos.locale';

function initialLocale() {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved && DICTS[saved]) return saved;
  } catch { /* ignore */ }
  return (navigator.language || 'ru').toLowerCase().startsWith('en') ? 'en' : 'ru';
}

let locale = initialLocale();

export const getLocale = () => locale;

export function setLocale(next) {
  if (!DICTS[next]) return;
  locale = next;
  try { localStorage.setItem(KEY, next); } catch { /* ignore */ }
  document.documentElement.lang = next;
}

document.documentElement.lang = locale;

// t('tasks.count', { n: 3 }) – `{n}` placeholders; plural forms as arrays
// [one, few, many] (ru) / [one, other] (en) selected by the `n` param.
export function t(key, params = {}) {
  let value = DICTS[locale][key] ?? DICTS.en[key] ?? key;
  if (Array.isArray(value)) {
    const rule = new Intl.PluralRules(locale).select(Number(params.n ?? 0));
    const index = locale === 'ru' ? { one: 0, few: 1, many: 2, other: 2 }[rule] : rule === 'one' ? 0 : 1;
    value = value[index] ?? value[value.length - 1];
  }
  return String(value).replace(/\{(\w+)\}/g, (_, name) => (params[name] ?? `{${name}}`));
}

// Enum/code label with a readable fallback for codes the dictionary does not know.
export function code(group, value) {
  if (value == null || value === '') return '—';
  const key = `${group}.${value}`;
  const hit = DICTS[locale][key] ?? DICTS.en[key];
  return hit ?? String(value).toLowerCase().replaceAll('_', ' ').replace(/^\w/, (c) => c.toUpperCase());
}

// Server clock alignment: relative times use the server's notion of "now" so a
// phone with a skewed clock (or a test fixture) stays consistent with the plan.
let clockOffset = 0;
export function setServerNow(value) {
  const d = new Date(value);
  if (!Number.isNaN(d.getTime())) clockOffset = d.getTime() - Date.now();
}
export const now = () => new Date(Date.now() + clockOffset);

const date = (value) => {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
};

export function fmtTime(value) {
  const d = date(value);
  return d ? new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' }).format(d) : '—';
}

export function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export function fmtDay(value, now = now_()) {
  const d = date(value);
  if (!d) return '—';
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return t('day.today');
  if (sameDay(d, tomorrow)) return t('day.tomorrow');
  if (sameDay(d, yesterday)) return t('day.yesterday');
  const label = new Intl.DateTimeFormat(locale, { weekday: 'short', day: 'numeric', month: 'short' }).format(d);
  return label.charAt(0).toUpperCase() + label.slice(1);
}

export function fmtDateTime(value, now = now_()) {
  const d = date(value);
  return d ? `${fmtDay(d, now)}, ${fmtTime(d)}` : '—';
}

export function fmtDuration(minutes) {
  if (minutes == null || minutes === '') return '—';
  const m = Math.round(Number(minutes));
  const h = Math.floor(m / 60);
  const r = m % 60;
  if (!h) return t('dur.m', { m });
  return r ? t('dur.hm', { h, m: r }) : t('dur.h', { h });
}

export function fmtRelative(value, now = now_()) {
  const d = date(value);
  if (!d) return '';
  const diffMin = Math.round((d - now) / 60000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' });
  const abs = Math.abs(diffMin);
  if (abs < 60) return rtf.format(diffMin, 'minute');
  if (abs < 48 * 60) return rtf.format(Math.round(diffMin / 60), 'hour');
  return rtf.format(Math.round(diffMin / 1440), 'day');
}

export function dayKey(value) {
  const d = date(value);
  return d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` : '';
}

function now_() { return now(); }
