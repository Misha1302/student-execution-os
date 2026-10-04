import { isNative, clearDeviceAlarms, clearExecutionNotification } from './native.js';
import { credentialStore } from './device-storage.js';
import { clearCaptureDrafts } from './capture-session.js';
import { onlineOperationAllowed } from './update-compatibility.js';

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
  session.server = isNative() ? normalizeServer(await credentialStore.get(KEYS.server)) : '';
  session.token = await credentialStore.get(KEYS.token);
  try { session.user = JSON.parse((await credentialStore.get(KEYS.user)) || 'null'); } catch { session.user = null; }
}

// Device alarms belong to one account on one server. The stored credentials change
// first and the alarms are cleared after: a background alarm sync that finishes in
// between then sees the new owner and discards the old account's list.
export async function setServer(url) {
  const next = normalizeServer(url);
  const changed = Boolean(session.server && session.server !== next);
  session.server = next;
  await credentialStore.set(KEYS.server, session.server || null);
  if (changed) {
    clearCaptureDrafts(localStorage);
    session.token = null;
    session.user = null;
    await credentialStore.set(KEYS.token, null);
    await credentialStore.set(KEYS.user, null);
    await clearDeviceAlarms();
    await clearExecutionNotification();
  }
}

export async function setAuth(token, user) {
  const ownershipChanged = Boolean(session.token && (!token || session.user?.account_id !== user?.account_id));
  session.token = token;
  session.user = user;
  // Signed-in state is "user, then token"; signing out removes the token first. Native
  // background work treats a token without a user as signed out (SessionCredentials).
  if (token) {
    await credentialStore.set(KEYS.user, user ? JSON.stringify(user) : null);
    await credentialStore.set(KEYS.token, token);
  } else {
    await credentialStore.set(KEYS.token, token);
    await credentialStore.set(KEYS.user, user ? JSON.stringify(user) : null);
  }
  if (ownershipChanged) {
    clearCaptureDrafts(localStorage);
    await clearDeviceAlarms();
    await clearExecutionNotification();
  }
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
  if (!onlineOperationAllowed(path)) throw new ApiError('Update required before online operations can continue', { code: 'UNSUPPORTED_CLIENT' });
  const base = server ?? session.server;
  if (isNative() && !base) throw new ApiError('No server configured', { code: 'NO_SERVER' });
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  // A credential belongs to exactly one server: never present it to another host
  // (for example while probing or signing in to a candidate server).
  const ownServer = base === session.server;
  if (session.token && ownServer) headers.Authorization = `Bearer ${session.token}`;
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
    if (response.status === 401 && err.code === 'UNAUTHENTICATED' && session.token && ownServer && !path.startsWith('/api/v1/auth/')) {
      await clearAuth();
      listeners.forEach((fn) => fn());
    }
    throw err;
  }
  return payload;
}


export async function apiUpload(path, blob, {
  mimeType = 'application/octet-stream', filename = null, timeoutMs = 60000,
  method = 'PUT', extraHeaders = {},
} = {}) {
  if (!onlineOperationAllowed(path)) throw new ApiError('Update required before online operations can continue', { code: 'UNSUPPORTED_CLIENT' });
  const base = session.server;
  if (isNative() && !base) throw new ApiError('No server configured', { code: 'NO_SERVER' });
  const headers = { Accept: 'application/json', 'Content-Type': mimeType };
  if (filename) headers['X-Filename'] = String(filename).slice(0, 255);
  Object.assign(headers, extraHeaders);
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${base}${path}`, { method, headers, body: blob, signal: controller.signal });
  } catch (err) {
    throw new ApiError(err?.name === 'AbortError' ? 'Request timed out' : 'Network unavailable', { code: 'NETWORK', retryable: true });
  } finally { clearTimeout(timer); }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = payload?.error || {};
    throw new ApiError(error.message || `HTTP ${response.status}`, { code: error.code || `HTTP_${response.status}`, status: response.status, retryable: Boolean(error.retryable) });
  }
  return payload;
}

export async function apiBlob(path, { timeoutMs = 60000 } = {}) {
  if (!onlineOperationAllowed(path)) throw new ApiError('Update required before online operations can continue', { code: 'UNSUPPORTED_CLIENT' });
  const base = session.server;
  if (isNative() && !base) throw new ApiError('No server configured', { code: 'NO_SERVER' });
  const headers = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(`${base}${path}`, { headers, signal: controller.signal });
  } catch (err) {
    throw new ApiError(err?.name === 'AbortError' ? 'Request timed out' : 'Network unavailable', { code: 'NETWORK', retryable: true });
  } finally { clearTimeout(timer); }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const error = payload?.error || {};
    throw new ApiError(error.message || `HTTP ${response.status}`, { code: error.code || `HTTP_${response.status}`, status: response.status, retryable: Boolean(error.retryable) });
  }
  return response.blob();
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
