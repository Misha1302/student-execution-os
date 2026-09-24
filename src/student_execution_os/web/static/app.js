import { session, restoreSession, refreshHealth, onUnauthenticated } from './js/api.js';
import { t, fmtTime, getLocale } from './js/i18n.js';
import { $, esc, icon, toast, errorMessage, closeTopSheet, closeAllSheets } from './js/ui.js';
import { isNative, onBackButton, exitApp, onResume, hideSplash } from './js/native.js';
import { applyTheme } from './js/theme.js';
import { peek, load } from './js/store.js';
import { shell } from './js/actions.js';
import { compose, composers } from './js/compose.js';

import today from './js/views/today.js';
import plan from './js/views/plan.js';
import tasks from './js/views/tasks.js';
import task from './js/views/task-detail.js';
import more, { MORE_ITEMS } from './js/views/more.js';
import calendar, { eventSheet } from './js/views/calendar.js';
import notifications from './js/views/notifications.js';
import evidence from './js/views/evidence.js';
import places from './js/views/places.js';
import assistant from './js/views/assistant.js';
import settings from './js/views/settings.js';
import welcome from './js/views/welcome.js';

const VIEWS = { today, plan, tasks, task, more, calendar, notifications, evidence, places, assistant, settings, welcome };

let route = { name: 'today', params: [], query: {} };
let current = null; // { view, data, stale, fetchedAt }
let renderSeq = 0;

// ---- routing ---------------------------------------------------------------------

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const [name, ...params] = path.split('/').filter(Boolean).map(decodeURIComponent);
  return { name: VIEWS[name] ? name : 'today', params, query: Object.fromEntries(new URLSearchParams(qs)) };
}

function go(name, { params = [], step, replace = false } = {}) {
  const qs = step ? `?step=${encodeURIComponent(step)}` : '';
  const hash = `#/${[name, ...params.map(encodeURIComponent)].join('/')}${qs}`;
  if (location.hash === hash) { render(); return; }
  if (replace) { history.replaceState(null, '', hash); route = parseHash(); render(); return; }
  location.hash = hash;
}

shell.go = (name, opts = {}) => go(name, { step: opts.step, params: opts.params || [] });
shell.rerender = (fresh = true) => render({ fresh });

const needsLogin = () => session.authMode === 'session' && !session.token;

// ---- chrome ------------------------------------------------------------------------

function buildTabbar() {
  const tab = (id, ic, extra = '') => `<button class="tab ${extra}" data-nav="${id}">${icon(ic)}<span>${esc(t(`nav.${id}`))}</span></button>`;
  $('#tabbar').innerHTML = `
    <div class="rail-brand">${icon('today')}<span>${esc(t('app.short'))}</span></div>
    ${tab('today', 'today')}
    ${tab('plan', 'plan')}
    <button class="fab" data-action="compose" aria-label="${esc(t('compose.title'))}">${icon('plus')}<span class="rail-only">${esc(t('compose.new'))}</span></button>
    ${tab('tasks', 'tasks')}
    ${tab('more', 'more', 'mobile-only')}
    <div class="rail-group">${MORE_ITEMS.map(([id, ic]) => tab(id, ic, 'rail-only')).join('')}</div>`;
}

function relabel() {
  buildTabbar();
  document.documentElement.lang = getLocale();
  $('#back-button').setAttribute('aria-label', t('common.back'));
  $('#refresh-button').setAttribute('aria-label', t('common.refresh'));
}

function updateChrome(view) {
  const bare = Boolean(view.bare);
  document.body.classList.toggle('bare', bare);
  const detail = Boolean(view.detail);
  $('#back-button').hidden = !detail;
  $('#page-title').textContent = view.title();
  const subtitle = view.id === 'today' ? capitalize(new Intl.DateTimeFormat(getLocale(), { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date())) : '';
  $('#page-subtitle').textContent = subtitle;
  $('#page-subtitle').hidden = !subtitle;
  document.title = `${view.title()} · ${t('app.name')}`;
  const active = view.id === 'task' ? 'tasks' : view.id;
  document.querySelectorAll('#tabbar [data-nav]').forEach((b) => {
    const on = b.dataset.nav === active || (b.classList.contains('mobile-only') && view.tab === 'more' && b.dataset.nav === 'more');
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
}

const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function setOffline(stale, fetchedAt) {
  const chip = $('#offline-chip');
  chip.hidden = !stale;
  chip.textContent = stale ? t('offline.chip', { time: fetchedAt ? fmtTime(fetchedAt) : '—' }) : '';
}

function skeleton() {
  return `<div class="view skeleton-view">${'<div class="skeleton"></div>'.repeat(4)}</div>`;
}

// ---- render ------------------------------------------------------------------------

async function render({ fresh = false, reuse = false } = {}) {
  const seq = ++renderSeq;
  const view = VIEWS[route.name] || today;
  if (!view.bare && needsLogin()) { go('welcome', { replace: true }); return; }
  const workspace = $('#workspace');
  updateChrome(view);
  workspace.dataset.view = view.id;
  let result;
  if (reuse && current?.view === view) {
    result = current;
  } else {
    workspace.dataset.viewState = 'loading';
    workspace.setAttribute('aria-busy', 'true');
    if (!workspace.querySelector(`.view[data-for="${view.id}"]`)) workspace.innerHTML = skeleton();
    try {
      result = await view.load({ fresh, params: route.params, query: route.query });
    } catch (err) {
      if (seq !== renderSeq) return;
      workspace.dataset.viewState = 'error';
      workspace.setAttribute('aria-busy', 'false');
      workspace.innerHTML = `<div class="view"><div class="empty error-state">${icon('alert', 'empty-icon')}
        <strong>${esc(t('err.loadTitle'))}</strong><p>${esc(errorMessage(err))}</p>
        <button class="button primary" data-action="retry">${esc(t('common.retry'))}</button></div></div>`;
      return;
    }
  }
  if (seq !== renderSeq) return;
  current = { view, ...result };
  const ctx = context();
  const container = document.createElement('div');
  container.className = 'view';
  container.dataset.for = view.id;
  container.innerHTML = view.render(result.data, route.params.length ? route.params : route.query, ctx);
  applyDynamicStyles(container);
  workspace.replaceChildren(container);
  view.mount?.(container, result.data, ctx);
  setOffline(result.stale, result.fetchedAt);
  workspace.dataset.viewState = 'ready';
  workspace.setAttribute('aria-busy', 'false');
  if (!reuse) window.scrollTo(0, 0);
}

// The CSP forbids inline style attributes, so sizes travel as data-* and are applied via CSSOM.
function applyDynamicStyles(root) {
  root.querySelectorAll('[data-w]').forEach((el) => { el.style.width = `${Number(el.dataset.w) || 0}%`; });
  root.querySelectorAll('[data-span]').forEach((el) => { el.style.setProperty('--span', el.dataset.span); });
}

function context() {
  return {
    view: current?.view,
    data: current?.data,
    params: route.params,
    rerender: () => render({ reuse: true }),
    refresh: () => render({ fresh: true }),
    relabel,
  };
}

// ---- global actions ---------------------------------------------------------------

async function openEvent(id) {
  let event = (peek('/api/v1/today')?.plan?.canonical_events || []).find((e) => e.id === id);
  if (!event) event = (await load('/api/v1/events')).data.find((e) => e.id === id);
  if (event) eventSheet(event);
}

const GLOBAL_ACTIONS = {
  compose: () => compose(),
  'compose-task': () => composers.task(),
  'open-task': (el) => go('task', { params: [el.dataset.id] }),
  'open-event': (el) => openEvent(el.dataset.id),
  retry: () => render({ fresh: true }),
};

document.addEventListener('click', (event) => {
  const el = event.target.closest('[data-nav],[data-action]');
  if (!el || el.disabled) return;
  if (el.hasAttribute('data-close-sheet')) closeAllSheets();
  if (el.dataset.nav) {
    event.preventDefault();
    closeAllSheets();
    go(el.dataset.nav);
    return;
  }
  const name = el.dataset.action;
  const handler = GLOBAL_ACTIONS[name] || current?.view?.actions?.[name];
  if (!handler) return;
  event.preventDefault();
  Promise.resolve(handler(el, context())).catch((err) => toast(errorMessage(err), { error: true }));
});

// ---- gestures & platform ----------------------------------------------------------

function installPullToRefresh() {
  const indicator = $('#ptr');
  let startY = null;
  let dy = 0;
  window.addEventListener('touchstart', (e) => {
    if (window.scrollY > 0 || document.querySelector('dialog[open]') || current?.view?.bare) { startY = null; return; }
    startY = e.touches[0].clientY;
    dy = 0;
  }, { passive: true });
  window.addEventListener('touchmove', (e) => {
    if (startY == null) return;
    dy = Math.max(0, e.touches[0].clientY - startY);
    const pull = Math.min(90, dy * 0.5);
    indicator.style.transform = `translateY(${pull}px) rotate(${pull * 4}deg)`;
    indicator.classList.toggle('armed', pull > 55);
    indicator.classList.toggle('visible', pull > 8);
  }, { passive: true });
  window.addEventListener('touchend', async () => {
    if (startY == null) return;
    const armed = dy * 0.5 > 55;
    startY = null;
    indicator.classList.remove('armed');
    if (armed) {
      indicator.classList.add('spinning');
      await render({ fresh: true });
      indicator.classList.remove('spinning');
    }
    indicator.classList.remove('visible');
    indicator.style.transform = '';
  });
}

function handleBack() {
  if (closeTopSheet()) return;
  const view = VIEWS[route.name];
  if (view?.detail) { history.length > 1 ? history.back() : go(view.tab || 'today'); return; }
  if (route.name !== 'today' && !view?.bare) { go('today'); return; }
  exitApp();
}

async function boot() {
  applyTheme();
  relabel();
  await restoreSession();
  $('#back-button').addEventListener('click', handleBack);
  $('#refresh-button').addEventListener('click', () => render({ fresh: true }));
  window.addEventListener('hashchange', () => { route = parseHash(); closeAllSheets(); render(); });
  onBackButton(handleBack);
  onUnauthenticated(() => { toast(t('err.unauth'), { error: true }); go('welcome'); });
  onResume(() => {
    if (!current || current.view.bare || document.querySelector('dialog[open]')) return;
    if (Date.now() - (current.fetchedAt || 0) > 60000) render({ fresh: true });
  });
  installPullToRefresh();

  route = parseHash();
  if (!(isNative() && !session.server)) {
    try {
      await refreshHealth();
    } catch (err) {
      // Offline start with a saved session: show cached read models.
      if (err.code === 'NETWORK' && session.token) session.authMode = 'session';
      else if (!isNative()) session.authMode = 'bound';
    }
  }
  if ((isNative() && !session.server) || needsLogin()) {
    if (route.name !== 'welcome') { history.replaceState(null, '', '#/welcome'); route = parseHash(); }
  } else if (route.name === 'welcome') {
    history.replaceState(null, '', '#/today');
    route = parseHash();
  }
  await render();
  hideSplash();
}

boot();
