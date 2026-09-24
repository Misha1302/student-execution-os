import { api, session } from './api.js';

// Read-model cache. Server responses are kept in memory for fast tab switches and
// mirrored to localStorage so a cold start without network can still show the last
// known state, clearly labelled as stale. Mutations always require the network.

const memory = new Map();
const PREFIX = 'seos.cache.';

function scope() {
  return `${session.server || 'same-origin'}|${session.user?.account_id || session.authMode || 'bound'}`;
}

function persist(path, entry) {
  try { localStorage.setItem(PREFIX + path, JSON.stringify({ scope: scope(), ...entry })); } catch { /* quota/private mode */ }
}

function restore(path) {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFIX + path) || 'null');
    return raw && raw.scope === scope() ? raw : null;
  } catch { return null; }
}

// Returns { data, stale, fetchedAt }.
export async function load(path, { fresh = false } = {}) {
  const hit = memory.get(path);
  if (hit && !fresh) return { ...hit, stale: false };
  try {
    const data = await api(path);
    const entry = { data, fetchedAt: Date.now() };
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

export function peek(path) { return memory.get(path)?.data; }
