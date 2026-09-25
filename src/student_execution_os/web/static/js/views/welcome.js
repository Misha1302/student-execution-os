import { api, session, setAuth, setServer, probeServer, refreshHealth, defaultServer, normalizeServer } from '../api.js';
import { t, getLocale, setLocale, LOCALES } from '../i18n.js';
import { esc, icon, chipGroup, toast, errorMessage, setBusy } from '../ui.js';
import { isNative } from '../native.js';
import { clearAll } from '../store.js';
import { shell } from '../actions.js';

let mode = 'login';
// A server that passed the probe but has no signed-in account yet. The current
// server, token, user, cache and offline queue stay untouched until sign-in on the
// candidate succeeds, so a failed switch never leaves the app half-switched.
let candidate = null;

const targetServer = () => candidate?.url ?? session.server;

// Server validation messages are English; map the known auth ones to the UI language.
function authError(err) {
  const msg = String(err?.message || '');
  if (err?.code === 'UNAUTHENTICATED' && mode === 'login') return t('welcome.badCredentials');
  if (msg.includes('already taken')) return t('welcome.taken');
  if (msg.startsWith('password must')) return t('welcome.passwordShort');
  if (msg.startsWith('login must')) return t('welcome.loginInvalid');
  if (msg.includes('registration is closed')) return t('welcome.closed');
  return errorMessage(err);
}

function serverStep() {
  const value = session.server || defaultServer();
  return `
    <div class="welcome-card">
      <div class="brand-mark big">${icon('server')}</div>
      <h1>${esc(t('welcome.serverTitle'))}</h1>
      <p class="muted">${esc(t('welcome.serverBody'))}</p>
      <form class="form" data-form="server">
        <label class="field"><span>${esc(t('welcome.serverLabel'))}</span>
          <input name="server" type="url" inputmode="url" autocapitalize="off" autocomplete="url" spellcheck="false"
            placeholder="https://example.com" value="${esc(value)}" required></label>
        <button class="button primary wide" type="submit">${esc(t('common.continue'))}</button>
        <p class="help">${esc(t('welcome.serverHelp'))}</p>
      </form>
    </div>`;
}

function authStep() {
  const register = mode === 'register';
  return `
    <div class="welcome-card">
      <div class="brand-mark big">${icon('today')}</div>
      <h1>${esc(t('app.name'))}</h1>
      <p class="muted">${esc(t('welcome.tagline'))}</p>
      ${(candidate ? candidate.registrationOpen : session.registrationOpen) ? chipGroup('auth-mode', [['login', t('welcome.login')], ['register', t('welcome.register')]], mode) : ''}
      <form class="form" data-form="auth">
        <label class="field"><span>${esc(t('welcome.loginLabel'))}</span>
          <input name="login" autocomplete="username" autocapitalize="off" spellcheck="false" required minlength="3" maxlength="64"
            value="${esc(candidate ? '' : (session.user?.login || ''))}"></label>
        <label class="field"><span>${esc(t('welcome.password'))}</span>
          <input name="password" type="password" autocomplete="${register ? 'new-password' : 'current-password'}" required minlength="${register ? 8 : 1}"></label>
        ${register ? `<label class="field"><span>${esc(t('welcome.passwordRepeat'))}</span>
          <input name="password2" type="password" autocomplete="new-password" required minlength="8"></label>
          <p class="help">${esc(t('welcome.registerHelp'))}</p>` : ''}
        <button class="button primary wide" type="submit">${esc(register ? t('welcome.createAccount') : t('welcome.signIn'))}</button>
      </form>
      ${isNative() ? `<button class="link" data-action="welcome-server">${icon('server')} ${esc(targetServer())}</button>` : ''}
      ${candidate && session.token ? `<button class="link" data-action="welcome-keep-server">${esc(t('welcome.keepServer', { server: session.server }))}</button>` : ''}
      <div class="welcome-locale">${chipGroup('welcome-locale', LOCALES, getLocale())}</div>
    </div>`;
}

export default {
  id: 'welcome',
  bare: true,
  title: () => t('app.name'),
  load: async () => ({ data: null, stale: false }),
  render(_data, params) {
    // On a fresh install the probed server is only a candidate until sign-in succeeds.
    const needServer = isNative() && (params.step === 'server' || (!session.server && !candidate));
    return `<div class="welcome">${needServer ? serverStep() : authStep()}</div>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name === 'auth-mode') { mode = e.detail.value; ctx.rerender(); }
      if (e.detail.name === 'welcome-locale') { setLocale(e.detail.value); ctx.relabel(); ctx.rerender(); }
    });
    root.querySelector('[data-form="server"]')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const button = e.submitter || e.target.querySelector('button');
      const url = normalizeServer(new FormData(e.target).get('server'));
      setBusy(button, true);
      try {
        const health = await probeServer(url);
        if (health.auth_mode !== 'session') {
          toast(t('welcome.boundServer'), { error: true });
          setBusy(button, false);
          return;
        }
        if (url === session.server) {
          candidate = null;
          session.authMode = health.auth_mode;
          session.registrationOpen = Boolean(health.registration_open);
          shell.go(session.token ? 'today' : 'welcome', { step: 'auth' });
          return;
        }
        candidate = { url, registrationOpen: Boolean(health.registration_open) };
        shell.go('welcome', { step: 'auth' });
      } catch (err) {
        toast(err.code === 'NETWORK' ? t('welcome.unreachable') : errorMessage(err), { error: true });
        setBusy(button, false);
      }
    });
    root.querySelector('[data-form="auth"]')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = new FormData(e.target);
      const button = e.submitter || e.target.querySelector('button[type="submit"]');
      if (mode === 'register' && form.get('password') !== form.get('password2')) {
        toast(t('welcome.passwordMismatch'), { error: true });
        return;
      }
      setBusy(button, true);
      try {
        if (!candidate) await refreshHealth().catch(() => {});
        const issued = await api(`/api/v1/auth/${mode === 'register' ? 'register' : 'login'}`, {
          method: 'POST',
          server: targetServer(),
          body: { login: form.get('login'), password: form.get('password'), device_label: isNative() ? 'android' : 'web' },
        });
        if (candidate) {
          // Commit the switch: setServer drops the previous identity, the read
          // cache is cleared; offline queues are keyed by server+account.
          await setServer(candidate.url);
          candidate = null;
          session.authMode = 'session';
        }
        await setAuth(issued.token, issued.user);
        clearAll();
        window.dispatchEvent(new CustomEvent('seos-signed-in'));
        shell.go('today');
      } catch (err) {
        toast(authError(err), { error: true });
        setBusy(button, false);
      }
    });
  },
  actions: {
    'welcome-server'() { shell.go('welcome', { step: 'server' }); },
    'welcome-keep-server'() { candidate = null; shell.go('today'); },
  },
};
