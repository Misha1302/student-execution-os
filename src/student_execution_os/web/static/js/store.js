import { api, session } from './api.js';
import { readQueue } from './sync.js';
import { project } from './overlay.js';
import { readModelCache } from './device-storage.js';

// Read-model cache. Server responses are kept in memory for fast tab switches and
// mirrored to localStorage so a cold start without network can still show the last
// known state, clearly labelled as stale. The cached responses are never edited:
// every read is passed through the overlay of queued changes (overlay.js), so what
// the user just did offline is visible everywhere immediately.

const memory = new Map();
// At a cold start the last saved state is shown at once and refreshed right after,
// instead of a skeleton for as long as a bad network takes to fail.
let cacheFirst = false;
export function setCacheFirst(on) { cacheFirst = Boolean(on); }

function scope() {
  return `${session.server || 'same-origin'}|${session.user?.account_id || session.authMode || 'bound'}`;
}

function persist(path, entry) {
  try { readModelCache.write(path, entry); } catch { /* quota/private mode */ }
}

function restore(path) {
  return readModelCache.read(path, scope());
}

function view(path, entry, stale) {
  return { data: project(path, entry.data, readQueue(), { fetchedAt: entry.fetchedAt }), fetchedAt: entry.fetchedAt, stale };
}

// Returns { data, stale, fetchedAt }. `fresh` asks the server; `cached` returns the
// last known response (plus queued changes) without touching the network at all.
export async function load(path, { fresh = false, cached = false } = {}) {
  const currentScope = scope();
  const candidate = memory.get(path);
  const hit = candidate?.scope === currentScope ? candidate : null;
  if (candidate && !hit) memory.delete(path);
  if (hit && !fresh && !hit.outdated) return view(path, hit, Boolean(hit.stale));
  if (cached || (cacheFirst && !fresh)) {
    const saved = restore(path);
    if (saved) return view(path, saved, true);
  }
  // The fetch start, not its end, is what the response can already contain.
  const requestedAt = Date.now();
  try {
    const data = await api(path);
    const entry = { scope: currentScope, data, fetchedAt: requestedAt };
    memory.set(path, entry);
    persist(path, entry);
    return view(path, entry, false);
  } catch (err) {
    if (err.code !== 'NETWORK') throw err;
    const saved = hit || restore(path);
    if (!saved) throw err;
    memory.set(path, { ...saved, scope: currentScope, stale: true, outdated: false });
    return view(path, saved, true);
  }
}

// The next load() of every response asks the server again. Until the new response
// arrives, peek() keeps answering with the one the screen on display was drawn from:
// emptying it made «Маршрут» right after a sync answer report no places at all.
export function invalidate() { for (const entry of memory.values()) entry.outdated = true; }

export function clearAll() {
  memory.clear();
  try { readModelCache.clear(); } catch { /* ignore */ }
}

// The last loaded response for `path` with queued changes applied (no network).
export function peek(path) {
  const hit = memory.get(path);
  if (hit?.scope !== scope()) return undefined;
  return project(path, hit.data, readQueue(), { fetchedAt: hit.fetchedAt });
}
