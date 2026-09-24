import { api, session } from './api.js';

// Read-model cache. Server responses are kept in memory for fast tab switches and
// mirrored to localStorage so a cold start without network can still show the last
// known state, clearly labelled as stale. Offline-capable mutations use sync.js.

const memory = new Map();
const PREFIX = 'seos.cache.';

function scope() {
  return `${session.server || 'same-origin'}|${session.user?.account_id || session.authMode || 'bound'}`;
}

function persist(path, entry) {
  try { localStorage.setItem(PREFIX + path, JSON.stringify(entry)); } catch { /* quota/private mode */ }
}

function restore(path) {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFIX + path) || 'null');
    return raw && raw.scope === scope() ? raw : null;
  } catch { return null; }
}

// Returns { data, stale, fetchedAt }.
export async function load(path, { fresh = false } = {}) {
  const currentScope = scope();
  const candidate = memory.get(path);
  const hit = candidate?.scope === currentScope ? candidate : null;
  if (candidate && !hit) memory.delete(path);
  if (hit && !fresh) return { ...hit, stale: false };
  try {
    const data = await api(path);
    const entry = { scope: currentScope, data, fetchedAt: Date.now() };
    memory.set(path, entry);
    persist(path, entry);
    return { ...entry, stale: false };
  } catch (err) {
    if (err.code !== 'NETWORK') throw err;
    const cached = hit || restore(path);
    if (!cached) throw err;
    return { data: cached.data, fetchedAt: cached.fetchedAt, stale: true };
  }
}

export function invalidate() { memory.clear(); }

export function clearAll() {
  memory.clear();
  try {
    Object.keys(localStorage).filter((k) => k.startsWith(PREFIX)).forEach((k) => localStorage.removeItem(k));
  } catch { /* ignore */ }
}

export function peek(path) {
  const hit = memory.get(path);
  return hit?.scope === scope() ? hit.data : undefined;
}

export function upsertCachedTask(task) {
  if (!task?.id) return;
  const path = '/api/v1/tasks';
  const inMemory = memory.get(path);
  const current = (inMemory?.scope === scope() ? inMemory : null) || restore(path);
  if (!current || !Array.isArray(current.data)) return;
  const data = [...current.data];
  const index = data.findIndex((item) => item.id === task.id);
  if (index >= 0) data[index] = { ...data[index], ...task };
  else data.unshift(task);
  const entry = { scope: scope(), data, fetchedAt: current.fetchedAt || Date.now() };
  memory.set(path, entry);
  persist(path, entry);
}
