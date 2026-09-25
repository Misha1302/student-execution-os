import { t, code } from './i18n.js';
import { haptic } from './native.js';

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

// Stroke icons (24px grid, currentColor). Kept inline: no network, no icon font.
const PATHS = {
  today: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  plan: '<rect x="3" y="4" width="18" height="17" rx="3"/><path d="M3 9h18M8 2v4M16 2v4M7 13h5M7 17h9"/>',
  tasks: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3.5 6 1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5M3.5 18l1.5 1.5 2.5-2.5"/>',
  more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  calendar: '<rect x="3" y="4" width="18" height="17" rx="3"/><path d="M3 9h18M8 2v4M16 2v4"/>',
  bell: '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  evidence: '<path d="M4 5h16M4 12h16M4 19h10"/><circle cx="19" cy="19" r="2"/>',
  place: '<path d="M12 21s-7-6.2-7-12a7 7 0 1 1 14 0c0 5.8-7 12-7 12Z"/><circle cx="12" cy="9" r="2.5"/>',
  spark: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8Z"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6"/><circle cx="12" cy="17.2" r=".6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  route: '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8"/>',
  flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
  alert: '<path d="M12 3 2 20h20Z"/><path d="M12 10v4"/><circle cx="12" cy="17" r=".6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
  repeat: '<path d="M17 2l3 3-3 3"/><path d="M4 11V9a4 4 0 0 1 4-4h12"/><path d="M7 22l-3-3 3-3"/><path d="M20 13v2a4 4 0 0 1-4 4H4"/>',
  event: '<rect x="3" y="4" width="18" height="17" rx="3"/><path d="M3 9h18M8 2v4M16 2v4"/><rect x="7" y="12" width="4" height="4" rx="1"/>',
  task: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
};

export function icon(name, cls = '') {
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${PATHS[name] || ''}</svg>`;
}

// ---- status / risk primitives ---------------------------------------------------

export const statusClass = (s) => (s === 'FEASIBLE' ? 'status-feasible' : s === 'INFEASIBLE' ? 'status-infeasible' : 'status-unknown');
export const statusIcon = (s) => (s === 'FEASIBLE' ? 'check' : s === 'INFEASIBLE' ? 'x' : 'question');

const RISK_TONE = {
  SAFE: 'ok', START_SOON: 'warn', AT_RISK: 'danger', CRITICAL: 'danger',
  IMPOSSIBLE: 'danger', OVERDUE: 'danger', UNKNOWN: 'muted', NOT_APPLICABLE: 'muted',
};
export const riskTone = (state) => RISK_TONE[state] || 'muted';

export function chip(text, tone = '', extra = '') {
  return `<span class="chip ${tone ? `chip-${tone}` : ''}" ${extra}>${esc(text)}</span>`;
}

// "Risk unknown"/"not applicable" says nothing useful to a person; show risk only when it means something.
export function riskChip(risk) {
  const state = risk?.state || 'UNKNOWN';
  if (state === 'UNKNOWN' || state === 'NOT_APPLICABLE') return '';
  return chip(code('risk', state), riskTone(state), `data-risk="${esc(state)}"`);
}

export function kv(label, value, { raw = false } = {}) {
  return `<div class="kv"><dt>${esc(label)}</dt><dd>${raw ? value : esc(value ?? '—')}</dd></div>`;
}

export function empty(title, body = '', iconName = 'check') {
  return `<div class="empty">${icon(iconName, 'empty-icon')}<strong>${esc(title)}</strong>${body ? `<p>${esc(body)}</p>` : ''}</div>`;
}

export function sectionHead(title, action = '') {
  return `<div class="section-head"><h2>${esc(title)}</h2>${action}</div>`;
}

// ---- toast ------------------------------------------------------------------------

export function toast(message, { error = false, action = null } = {}) {
  const region = $('#toast-region');
  const el = document.createElement('div');
  el.className = `toast${error ? ' error' : ''}`;
  el.setAttribute('role', error ? 'alert' : 'status');
  el.innerHTML = `<span>${esc(message)}</span>${action ? `<button class="toast-action">${esc(action.label)}</button>` : ''}`;
  if (action) el.querySelector('button').addEventListener('click', () => { action.run(); el.remove(); });
  region.append(el);
  if (error) haptic('MEDIUM');
  setTimeout(() => el.classList.add('leaving'), 3800);
  setTimeout(() => el.remove(), 4200);
}

export function errorMessage(err) {
  const known = {
    NETWORK: t('err.network'),
    VERSION_CONFLICT: t('err.conflict'),
    UNAUTHENTICATED: t('err.unauth'),
    RATE_LIMITED: t('err.rate'),
    NOT_FOUND: t('err.notFound'),
    NO_SERVER: t('err.noServer'),
  };
  return known[err?.code] || err?.message || t('err.generic');
}

// ---- sheets (bottom sheet on phones, centered dialog on wide screens) -------------

const sheetStack = [];

export function openSheet({ title = '', eyebrow = '', body = '', actions = '', full = false, onClose } = {}) {
  const dialog = document.createElement('dialog');
  dialog.className = `sheet${full ? ' sheet-full' : ''}`;
  dialog.innerHTML = `
    <form method="dialog" class="sheet-frame">
      <div class="sheet-grabber" aria-hidden="true"></div>
      <header class="sheet-head">
        <div>${eyebrow ? `<p class="eyebrow">${esc(eyebrow)}</p>` : ''}<h2>${esc(title)}</h2></div>
        <button value="cancel" class="icon-button" aria-label="${esc(t('common.close'))}">${icon('x')}</button>
      </header>
      <div class="sheet-body">${body}</div>
      ${actions ? `<footer class="sheet-actions">${actions}</footer>` : ''}
    </form>`;
  document.body.append(dialog);
  sheetStack.push(dialog);
  dialog.addEventListener('close', () => {
    const i = sheetStack.indexOf(dialog);
    if (i >= 0) sheetStack.splice(i, 1);
    dialog.remove();
    onClose?.(dialog.returnValue);
  });
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close('cancel'); });
  enableSwipeDown(dialog);
  dialog.showModal();
  return dialog;
}

export function closeTopSheet() {
  // Leave the stack now: <dialog> fires "close" asynchronously, so waiting for the
  // event here would make closeAllSheets() spin forever.
  const top = sheetStack.pop();
  if (!top) return false;
  if (top.open) top.close('cancel');
  else top.remove();
  return true;
}

export const closeAllSheets = () => { while (closeTopSheet()) { /* drain */ } };

function enableSwipeDown(dialog) {
  const frame = dialog.querySelector('.sheet-frame');
  const grab = dialog.querySelector('.sheet-grabber');
  const head = dialog.querySelector('.sheet-head');
  let startY = null;
  const start = (e) => { startY = e.touches[0].clientY; frame.style.transition = 'none'; };
  const move = (e) => {
    if (startY == null) return;
    const dy = Math.max(0, e.touches[0].clientY - startY);
    frame.style.transform = `translateY(${dy}px)`;
  };
  const end = (e) => {
    if (startY == null) return;
    const dy = e.changedTouches[0].clientY - startY;
    frame.style.transition = '';
    frame.style.transform = '';
    startY = null;
    if (dy > 90) dialog.close('cancel');
  };
  for (const el of [grab, head]) {
    el.addEventListener('touchstart', start, { passive: true });
    el.addEventListener('touchmove', move, { passive: true });
    el.addEventListener('touchend', end);
  }
}

export function confirmSheet({ title, body, confirmLabel, danger = false, cancelLabel = t('common.cancel') }) {
  return new Promise((resolve) => {
    const dialog = openSheet({
      title,
      body: `<div class="confirm-body">${body}</div>`,
      actions: `<button value="cancel" class="button ghost">${esc(cancelLabel)}</button>
                <button value="confirm" class="button ${danger ? 'danger' : 'primary'}" data-confirm>${esc(confirmLabel)}</button>`,
      onClose: (value) => resolve(value === 'confirm'),
    });
    dialog.querySelector('[data-confirm]').focus();
  });
}

// Action sheet: list of big tappable rows. Resolves with the chosen id or null.
export function actionSheet({ title, items }) {
  return new Promise((resolve) => {
    let chosen = null;
    const dialog = openSheet({
      title,
      body: `<div class="menu-list">${items.map((it) => `
        <button type="button" class="menu-row" data-choice="${esc(it.id)}">
          <span class="menu-icon tone-${esc(it.tone || 'accent')}">${icon(it.icon)}</span>
          <span class="menu-copy"><strong>${esc(it.label)}</strong>${it.hint ? `<small>${esc(it.hint)}</small>` : ''}</span>
          ${icon('chevron', 'menu-chevron')}
        </button>`).join('')}</div>`,
      onClose: () => resolve(chosen),
    });
    dialog.addEventListener('click', (e) => {
      const row = e.target.closest('[data-choice]');
      if (!row) return;
      chosen = row.dataset.choice;
      dialog.close('choice');
    });
  });
}

// ---- form helpers --------------------------------------------------------------

export function chipGroup(name, options, selected) {
  return `<div class="chip-group" role="radiogroup" data-chip-group="${esc(name)}">${options.map(([value, label]) =>
    `<button type="button" role="radio" class="chip-toggle${String(value) === String(selected) ? ' on' : ''}" aria-checked="${String(value) === String(selected)}" data-value="${esc(value)}">${esc(label)}</button>`).join('')}</div>`;
}

export function chipValue(root, name) {
  return root.querySelector(`[data-chip-group="${name}"] .chip-toggle.on`)?.dataset.value ?? null;
}

// Single delegated handler makes every chip group behave like a radio group.
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.chip-toggle');
  if (!btn) return;
  const group = btn.closest('[data-chip-group]');
  if (!group) return; // a plain answer chip: its own handler reacts
  group.querySelectorAll('.chip-toggle').forEach((b) => { b.classList.toggle('on', b === btn); b.setAttribute('aria-checked', String(b === btn)); });
  group.dispatchEvent(new CustomEvent('chipchange', { bubbles: true, detail: { name: group.dataset.chipGroup, value: btn.dataset.value } }));
  haptic('LIGHT');
});

// <input type=datetime-local> value <-> ISO instant (with the device's offset).
export function localInputValue(date) {
  if (!date) return '';
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function isoFromLocalInput(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
  button.classList.toggle('busy', busy);
}
