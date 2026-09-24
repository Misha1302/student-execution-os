import { isNative, prefGet, prefSet } from './native.js';

// Session state. In the browser the page is served by the API host itself, so the
// base URL is same-origin (''). The Android app keeps a user-chosen server URL.
export const session = {
  server: '',
  token: null,
  user: null,
  authMode: null, // 'session' | 'bound' once /health answered
  registrationOpen: false,
};

const KEYS = { server: 'seos.server', token: 'seos.token', user: 'seos.user' };
const listeners = new Set();

export function onUnauthenticated(fn) { listeners.add(fn); }

export function normalizeServer(value) {
  let url = String(value || '').trim();
  if (!url) return '';
  if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
  return url.replace(/\/+$/, '');
}

export function defaultServer() {
  return normalizeServer(window.SEOS_CONFIG?.defaultServerUrl || '');
}

export async function restoreSession() {
  session.server = isNative() ? normalizeServer(await prefGet(KEYS.server)) : '';
  session.token = await prefGet(KEYS.token);
  try { session.user = JSON.parse((await prefGet(KEYS.user)) || 'null'); } catch { session.user = null; }
}

export async function setServer(url) {
  const next = normalizeServer(url);
  const changed = Boolean(session.server && session.server !== next);
  session.server = next;
  await prefSet(KEYS.server, session.server || null);
  if (changed) {
    session.token = null;
    session.user = null;
    await prefSet(KEYS.token, null);
    await prefSet(KEYS.user, null);
  }
}

export async function setAuth(token, user) {
  session.token = token;
  session.user = user;
  await prefSet(KEYS.token, token);
  await prefSet(KEYS.user, user ? JSON.stringify(user) : null);
}

export async function clearAuth() { await setAuth(null, null); }

export class ApiError extends Error {
  constructor(message, { code, status, retryable = false } = {}) {
    super(message);
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

export async function api(path, { method = 'GET', body, server, timeoutMs = 20000 } = {}) {
  const base = server ?? session.server;
  if (isNative() && !base) throw new ApiError('No server configured', { code: 'NO_SERVER' });
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${base}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    throw new ApiError(err?.name === 'AbortError' ? 'Request timed out' : 'Network unavailable', { code: 'NETWORK', retryable: true });
  } finally {
    clearTimeout(timer);
  }
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('json') ? await response.json().catch(() => null) : await response.text();
  if (!response.ok) {
    const error = payload?.error || {};
    const err = new ApiError(error.message || `HTTP ${response.status}`, {
      code: error.code || `HTTP_${response.status}`,
      status: response.status,
      retryable: Boolean(error.retryable),
    });
    if (response.status === 401 && err.code === 'UNAUTHENTICATED' && session.token && !path.startsWith('/api/v1/auth/')) {
      await clearAuth();
      listeners.forEach((fn) => fn());
    }
    throw err;
  }
  return payload;
}

export async function probeServer(url) {
  const health = await api('/api/v1/health', { server: normalizeServer(url), timeoutMs: 8000 });
  if (health?.service !== 'student-execution-os') throw new ApiError('Not a Student Execution OS server', { code: 'NOT_SEOS' });
  if (Number(health.api_version) !== 1 || Number(health.sync_protocol) < 1) {
    throw new ApiError('Server version is not compatible with this app', { code: 'INCOMPATIBLE_SERVER' });
  }
  return health;
}

export async function refreshHealth() {
  const health = await api('/api/v1/health', { timeoutMs: 8000 });
  session.authMode = health.auth_mode || 'bound';
  session.registrationOpen = Boolean(health.registration_open);
  return health;
}
