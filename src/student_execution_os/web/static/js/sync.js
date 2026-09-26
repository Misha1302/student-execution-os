import { api, ApiError, session } from './api.js';

// Durable operation queue — the write half of offline-first.
//
// Every change the user makes (create, edit, start, done, «не сейчас», reschedule,
// archive, delete, events…) becomes one operation with a client-generated op_id.
// It is written to localStorage *before* the UI reports success and before any
// network call, so it survives a lost connection, a killed app and a restart.
// The UI shows the change at once through the overlay (overlay.js) and never waits
// for the network.
//
// Delivery is at-least-once and the server makes it exactly-once: /api/v1/sync
// records each op_id and replays its first result, so a retry after a lost
// response, or the same queue flushed from two tabs, cannot apply twice. Repeated
// taps on the same lifecycle button while an identical operation is still queued
// are collapsed here and never reach the server.
//
// Queue item states: PENDING (not confirmed yet), ACKED (the server applied it; kept
// briefly so cached read models older than the ack still show it), CONFLICT /
// REJECTED (the server refused; shown in the sync sheet until dismissed).

const PREFIX = 'seos.ops.';
const BATCH = 100;
const BACKOFF_MS = [2000, 5000, 15000, 30000, 60000];
const ACK_KEEP_MS = 24 * 3600 * 1000;
// Operations whose second copy would change nothing (or is an accidental double tap).
const IDEMPOTENT = new Set(['task.start', 'task.complete', 'task.cancel', 'task.reopen', 'task.archive', 'task.unarchive',
  'task.restore', 'task.delete', 'event.cancel', 'event.reopen', 'event.delete',
  'reminder.done', 'reminder.cancel', 'reminder.reopen', 'reminder.delete']);

let flushing = null;
let again = false;
let attempt = 0;
let retryTimer = null;

function scope() {
  return `${session.server || 'same-origin'}|${session.user?.account_id || session.authMode || 'bound'}`;
}

function key() { return PREFIX + scope(); }

export function readQueue() {
  try {
    const items = JSON.parse(localStorage.getItem(key()) || '[]');
    return Array.isArray(items) ? items : [];
  } catch { return []; }
}

function emit(name, detail) {
  try { globalThis.window?.dispatchEvent?.(new CustomEvent(name, { detail })); } catch { /* not in a browser */ }
}

function write(items) {
  const cutoff = Date.now() - ACK_KEEP_MS;
  const kept = items.filter((x) => x.state !== 'ACKED' || Number(x.acked_at || 0) > cutoff);
  localStorage.setItem(key(), JSON.stringify(kept));
  emit('seos-sync-state', syncState());
}

function id(prefix) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

export function lastSyncedAt() {
  try { return Number(localStorage.getItem(`seos.lastSync.${scope()}`)) || null; } catch { return null; }
}

export function syncState() {
  const items = readQueue();
  return {
    lastSyncedAt: lastSyncedAt(),
    pending: items.filter((x) => x.state === 'PENDING').length,
    conflicts: items.filter((x) => x.state === 'CONFLICT' || x.state === 'REJECTED').length,
    items: items.filter((x) => x.state !== 'ACKED'),
  };
}

// Queues one change and returns immediately; nothing here waits for the network.
// Throws only when the device cannot store the change (then nothing was accepted).
export function queueOperation(type, entityId, payload = {}) {
  const items = readQueue();
  if (IDEMPOTENT.has(type)) {
    const last = [...items].reverse().find((x) => x.state === 'PENDING' && x.operation?.entity_id === entityId);
    if (last && last.operation.type === type) return { op_id: last.operation.op_id, status: 'PENDING', duplicate: true };
  }
  const operation = { op_id: id('op'), type, entity_id: entityId, payload };
  const queued = { operation, state: 'PENDING', queued_at: new Date().toISOString(), result: null };
  try {
    write([...items, queued]);
  } catch {
    throw new ApiError('Unable to save the change on this device', { code: 'LOCAL_STORAGE' });
  }
  // Prove persistence before telling the user the change was accepted.
  if (!readQueue().some((x) => x.operation?.op_id === operation.op_id)) {
    throw new ApiError('Unable to save the change on this device', { code: 'LOCAL_STORAGE' });
  }
  scheduleFlush(0);
  return { op_id: operation.op_id, status: 'PENDING' };
}

export function scheduleFlush(delay) {
  clearTimeout(retryTimer);
  retryTimer = setTimeout(() => { retryTimer = null; flushSync().catch(() => {}); }, delay);
  retryTimer?.unref?.(); // never keeps a non-browser runtime (tests) alive
}

function backoff() {
  const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
  attempt += 1;
  scheduleFlush(delay);
}

async function flushOnce() {
  const items = readQueue();
  const pending = items.filter((x) => x.state === 'PENDING').slice(0, BATCH);
  if (!pending.length) return [];
  let response;
  try {
    response = await api('/api/v1/sync', { method: 'POST', body: { operations: pending.map((x) => x.operation) } });
  } catch (error) {
    // Network trouble, a server restart or an expired session: keep everything and
    // try again later. Nothing is dropped because a request failed.
    again = false;
    backoff();
    if (error.code === 'NETWORK' || error.retryable || !error.status || error.status >= 500) return [];
    // The server refused the whole request (not one operation): tell the user once
    // per streak; the changes stay queued and visible in the sync sheet.
    if (attempt === 1) emit('seos-sync-error', { error });
    throw error;
  }
  attempt = 0;
  try { localStorage.setItem(`seos.lastSync.${scope()}`, String(Date.now())); } catch { /* storage full */ }
  const byId = new Map((response.results || []).map((result) => [result.op_id, result]));
  const ackedAt = Date.now();
  // Re-read: the user may have queued more while the request was in flight.
  const next = readQueue().map((item) => {
    const result = byId.get(item.operation?.op_id);
    if (!result) return item;
    if (result.status === 'APPLIED' || result.status === 'NOOP') return { ...item, state: 'ACKED', acked_at: ackedAt, result };
    return { ...item, state: result.status, result };
  });
  write(next);
  const results = response.results || [];
  emit('seos-sync-applied', { results });
  if (results.length === pending.length && next.some((x) => x.state === 'PENDING')) again = true;
  return results;
}

// Sends what is queued. Concurrent callers share one run; a change queued during
// the run is sent right after it. Resolves with the results of the last batch.
export function flushSync() {
  if (flushing) { again = true; return flushing; }
  flushing = (async () => {
    let results = [];
    do {
      again = false;
      results = await flushOnce();
    } while (again);
    return results;
  })().finally(() => { flushing = null; });
  return flushing;
}

// Resolves with the state of one queued operation once the server answered it, or
// 'PENDING' after `timeoutMs` — used to word a confirmation ("added" vs "saved on
// this phone") without ever blocking the UI on the network.
export function settled(opId, timeoutMs = 1500) {
  const state = () => readQueue().find((x) => x.operation?.op_id === opId)?.state || 'ACKED';
  return new Promise((resolve) => {
    const done = (value) => { clearTimeout(timer); globalThis.window?.removeEventListener?.('seos-sync-state', check); resolve(value); };
    const check = () => { const now = state(); if (now !== 'PENDING') done(now); };
    const timer = setTimeout(() => done(state()), timeoutMs);
    globalThis.window?.addEventListener?.('seos-sync-state', check);
    check();
  });
}

export function discardSyncProblem(opId) {
  write(readQueue().filter((x) => x.operation?.op_id !== opId));
}

export function newEntityId(kind = 'task') { return id(kind); }

// Retry triggers: the network coming back, the app returning to the foreground
// (app.js) and a slow heartbeat while something is still queued.
if (globalThis.window?.addEventListener) {
  window.addEventListener('online', () => { attempt = 0; flushSync().catch(() => {}); });
  setInterval(() => {
    if (!flushing && !retryTimer && readQueue().some((x) => x.state === 'PENDING')) flushSync().catch(() => {});
  }, 30000)?.unref?.();
}
