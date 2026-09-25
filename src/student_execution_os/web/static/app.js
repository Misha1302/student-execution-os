import { api, session, restoreSession, refreshHealth, onUnauthenticated } from './js/api.js';
import { t, fmtTime, getLocale } from './js/i18n.js';
import { $, esc, icon, toast, errorMessage, closeTopSheet, closeAllSheets, openSheet, setBusy } from './js/ui.js';
import { isNative, onBackButton, exitApp, onResume, hideSplash, setupPush, prefSet, onAppLink } from './js/native.js';
import { applyTheme } from './js/theme.js';
import { peek, load, invalidate } from './js/store.js';
import { shell } from './js/actions.js';
import { composers } from './js/compose.js';
import { openCapture, deviceTimeZone } from './js/capture.js';
import { flushSync, syncState, discardSyncProblem } from './js/sync.js';

import today from './js/views/today.js';
import plan from './js/views/plan.js';
import tasks from './js/views/tasks.js';
import task from './js/views/task-detail.js';
import more, { MORE_ITEMS } from './js/views/more.js';
import calendar, { eventSheet } from './js/views/calendar.js';
import notifications from './js/views/notifications.js';
import evidence from './js/views/evidence.js';
import places from './js/views/places.js';
import settings from './js/views/settings.js';
import welcome from './js/views/welcome.js';

const VIEWS = { today, plan, tasks, task, more, calendar, notifications, evidence, places, settings, welcome };

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
    <button class="fab" data-action="compose" aria-label="${esc(t('capture.title'))}">${icon('plus')}<span class="rail-only">${esc(t('compose.new'))}</span></button>
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
  const syncing = syncState();
  chip.hidden = !stale && !syncing.pending && !syncing.conflicts;
  chip.classList.toggle('chip-danger', Boolean(syncing.conflicts));
  chip.textContent = syncing.conflicts ? t('sync.conflictChip', { n: syncing.conflicts })
    : syncing.pending ? t('sync.pendingChip', { n: syncing.pending })
      : stale ? t('offline.chip', { time: fetchedAt ? fmtTime(fetchedAt) : '—' }) : '';
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
    query: route.query,
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

// Pending and rejected offline operations stay visible until they are sent or the
// user dismisses them; nothing is dropped silently.
function syncSheet() {
  const { items } = syncState();
  const pending = items.filter((x) => x.state === 'PENDING');
  const problems = items.filter((x) => x.state !== 'PENDING');
  const titleOf = (op, item) => item.result?.entity?.title || op.payload?.title
    || (peek('/api/v1/tasks') || []).find((x) => x.id === op.entity_id)?.title || t('sync.someTask');
  const row = (item, problem) => {
    const op = item.operation || {};
    const detail = problem ? t(`sync.why.${item.state}`) : new Date(item.queued_at).toLocaleString();
    return `<article class="row"><span class="row-main"><strong>${esc(t(`sync.op.${op.type}`))} · ${esc(titleOf(op, item))}</strong>
      <small>${esc(detail)}</small></span>
      ${problem ? `<button type="button" class="button ghost" data-dismiss="${esc(op.op_id)}">${esc(t('sync.dismiss'))}</button>` : ''}</article>`;
  };
  const dialog = openSheet({
    title: t('sync.title'),
    body: `${!items.length ? `<p class="muted">${esc(t('sync.empty'))}</p>` : ''}
      ${pending.length ? `<h3>${esc(t('sync.pending'))}</h3><div class="list">${pending.map((x) => row(x, false)).join('')}</div>` : ''}
      ${problems.length ? `<h3>${esc(t('sync.problems'))}</h3><p class="help">${esc(t('sync.problemHelp'))}</p>
        <div class="list">${problems.map((x) => row(x, true)).join('')}</div>` : ''}`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.close'))}</button>
      ${pending.length ? `<button type="button" class="button primary" data-retry>${esc(t('sync.retry'))}</button>` : ''}`,
  });
  dialog.querySelectorAll('[data-dismiss]').forEach((button) => button.addEventListener('click', () => {
    discardSyncProblem(button.dataset.dismiss);
    button.closest('.row')?.remove();
  }));
  dialog.querySelector('[data-retry]')?.addEventListener('click', async (event) => {
    setBusy(event.currentTarget, true);
    try {
      await flushSync();
      if (syncState().pending) toast(t('sync.stillOffline'), { error: true });
    } catch (err) {
      toast(errorMessage(err), { error: true });
    }
    dialog.close('done');
    invalidate();
    render({ fresh: true });
  });
}

const GLOBAL_ACTIONS = {
  'sync-status': () => syncSheet(),
  compose: () => openCapture(),
  'compose-task': () => openCapture(),
  'compose-voice': () => openCapture({ listen: true }),
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

// Registers this device's FCM token with the signed-in account (no-op in browsers
// and in builds without Firebase configuration).
function registerPush() {
  if (!isNative() || !session.token) return;
  setupPush(async (token) => {
    if (!session.token) return;
    // This build renders reminder notifications itself, with working action buttons.
    const device = await api('/api/v1/mobile/devices', { method: 'POST', body: { token, label: 'Android', capabilities: ['reminder-actions-v1'] } });
    await prefSet('seos.pushDevice', JSON.stringify({ id: device.id, version: device.version }));
  }, (deepLink) => openRoute(deepLink || '/today')).catch((err) => console.warn('push setup failed', err));
}

// Reminder texts, quiet hours and "в 18:00" in typed tasks all use the account's
// time zone and language; keep them equal to the device's, once per session.
let preferencesSynced = false;
async function syncPreferences() {
  if (preferencesSynced || needsLogin()) return;
  try {
    const zone = deviceTimeZone();
    const prefs = await api('/api/v1/notification-preferences');
    if (prefs.timezone !== zone || prefs.locale !== getLocale()) {
      await api('/api/v1/notification-preferences', { method: 'PATCH', body: { timezone: zone, locale: getLocale() } });
    }
    const profile = await api('/api/v1/settings/planning-profile');
    if (profile.timezone !== zone) await api('/api/v1/settings/planning-profile', { method: 'PATCH', body: { timezone: zone, expected_version: profile.version } });
    preferencesSynced = true;
  } catch { /* offline or an older server: try again next start */ }
}

function openRoute(path) {
  closeAllSheets();
  const clean = String(path || 'today').replace(/^#?\/?/, '');
  location.hash = `#/${clean || 'today'}`;
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
    flushSync().catch(() => {});
    if (!current || current.view.bare || document.querySelector('dialog[open]')) return;
    // Notification buttons change tasks while the app is in the background.
    if (Date.now() - (current.fetchedAt || 0) > 5000) { invalidate(); render({ fresh: true }); }
  });
  installPullToRefresh();
  window.addEventListener('seos-sync-state', () => setOffline(Boolean(current?.stale), current?.fetchedAt));
  window.addEventListener('seos-push-received', (event) => {
    invalidate();
    toast(event.detail?.title || t('nav.notifications'));
    if (current && !current.view.bare) render({ fresh: true });
  });

  if (/^#\/?assistant/.test(location.hash)) history.replaceState(null, '', '#/today');
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
  await flushSync().catch(() => {});
  registerPush();
  syncPreferences();
  onAppLink(openRoute).catch(() => {});
  window.addEventListener('seos-signed-in', (event) => {
    preferencesSynced = false;
    registerPush();
    syncPreferences();
    flushSync().catch(() => {});
    // A brand-new account starts with its first task.
    if (event.detail?.firstRun) setTimeout(() => openCapture(), 300);
  });
  hideSplash();
}

boot();
