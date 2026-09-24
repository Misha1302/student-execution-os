import { api, ApiError, session } from './api.js';
import { upsertCachedTask } from './store.js';

const PREFIX = 'seos.ops.';
let flushing = null;

function scope() {
  return `${session.server || 'same-origin'}|${session.user?.account_id || session.authMode || 'bound'}`;
}

function key() { return PREFIX + scope(); }

function read() {
  try { return JSON.parse(localStorage.getItem(key()) || '[]'); } catch { return []; }
}

function write(items) {
  try { localStorage.setItem(key(), JSON.stringify(items)); } catch { /* surfaced by failed persistence check below */ }
  window.dispatchEvent(new CustomEvent('seos-sync-state', { detail: syncState() }));
}

function id(prefix) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

export function syncState() {
  const items = read();
  return {
    pending: items.filter((x) => x.state === 'PENDING').length,
    conflicts: items.filter((x) => x.state === 'CONFLICT' || x.state === 'REJECTED').length,
    items,
  };
}

export async function queueOperation(type, entityId, payload = {}, { optimisticTask } = {}) {
  const operation = { op_id: id('op'), type, entity_id: entityId, payload };
  const queued = { operation, state: 'PENDING', queued_at: new Date().toISOString(), result: null };
  const items = [...read(), queued];
  write(items);
  // Prove persistence before telling the user the action was accepted offline.
  if (!read().some((x) => x.operation?.op_id === operation.op_id)) throw new Error('Unable to persist offline change');
  if (optimisticTask) upsertCachedTask({ ...optimisticTask, _pending: true });
  const results = await flushSync();
  const own = results.find((x) => x.op_id === operation.op_id);
  if (own?.entity) upsertCachedTask(own.entity);
  if (own?.status === 'CONFLICT' || own?.status === 'REJECTED') {
    throw new ApiError(own.message || own.code || 'Sync conflict', {
      code: own.status === 'CONFLICT' ? 'VERSION_CONFLICT' : (own.code || 'SYNC_REJECTED'), status: 409,
    });
  }
  return own || { op_id: operation.op_id, status: 'PENDING', entity: optimisticTask || null, offline: true };
}

export async function flushSync() {
  if (flushing) return flushing;
  flushing = (async () => {
    const items = read();
    const pending = items.filter((x) => x.state === 'PENDING');
    if (!pending.length) return [];
    let response;
    try {
      response = await api('/api/v1/sync', { method: 'POST', body: { operations: pending.map((x) => x.operation) } });
    } catch (error) {
      if (error.code === 'NETWORK') return [];
      throw error;
    }
    const byId = new Map((response.results || []).map((result) => [result.op_id, result]));
    const next = [];
    for (const item of items) {
      const result = byId.get(item.operation?.op_id);
      if (!result) { next.push(item); continue; }
      if (result.entity?.kind === 'TASK') upsertCachedTask(result.entity);
      if (result.status === 'APPLIED' || result.status === 'NOOP') continue;
      next.push({ ...item, state: result.status, result });
    }
    write(next);
    return response.results || [];
  })().finally(() => { flushing = null; });
  return flushing;
}

export function discardSyncProblem(opId) {
  write(read().filter((x) => x.operation?.op_id !== opId));
}

export function newEntityId(kind = 'task') { return id(kind); }

window.addEventListener('online', () => { flushSync().catch(() => {}); });
