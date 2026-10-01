import assert from 'node:assert/strict';
import {
  BrowserOfflineOperationStore, BrowserReadModelCache, PreferencesCredentialStore,
} from '../../src/student_execution_os/web/static/js/device-storage.js';

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
  key(index) { return [...this.values.keys()][index] ?? null; }
  get length() { return this.values.size; }
}

// Queue continuity: entries written by the pre-boundary sync.js (raw localStorage
// keys) are read unchanged, so an app update needs no migration step.
const storage = new MemoryStorage();
storage.setItem('seos.ops.https://a.example|acc-1', JSON.stringify([{ operation: { op_id: 'legacy' }, state: 'PENDING' }]));
storage.setItem('seos.lastSync.https://a.example|acc-1', '1700');
const operations = new BrowserOfflineOperationStore(storage);
assert.equal(operations.read('https://a.example|acc-1')[0].operation.op_id, 'legacy');
assert.equal(operations.lastSyncedAt('https://a.example|acc-1'), 1700);

// Account and server scopes never see each other's queue.
operations.write('server-a|account-a', [{ operation: { op_id: 'one' }, state: 'PENDING' }]);
assert.equal(storage.getItem('seos.ops.server-a|account-a'), JSON.stringify([{ operation: { op_id: 'one' }, state: 'PENDING' }]));
assert.deepEqual(operations.read('server-a|account-b'), []);
assert.deepEqual(operations.read('server-b|account-a'), []);
operations.setLastSyncedAt('server-a|account-a', 42);
assert.equal(operations.lastSyncedAt('server-a|account-a'), 42);

// Corrupt queue data is never trusted as a queue.
storage.setItem('seos.ops.bad', '{"not":"a list"}');
assert.deepEqual(operations.read('bad'), []);

// A failed enqueue must surface to the caller (no silent "saved" over a lost op).
const full = new MemoryStorage();
full.setItem = () => { throw new Error('QuotaExceededError'); };
assert.throws(() => new BrowserOfflineOperationStore(full).write('s', [{ state: 'PENDING' }]), /Quota/);

// Read-model cache is scoped and its wipe leaves queue and credentials alone.
storage.setItem('seos.token', 'bearer');
const cache = new BrowserReadModelCache(storage);
cache.write('/api/v1/today', { scope: 'server-a|account-a', data: { title: 'private' }, fetchedAt: 1 });
assert.equal(cache.read('/api/v1/today', 'server-a|account-a').data.title, 'private');
assert.equal(cache.read('/api/v1/today', 'server-a|account-b'), null);
cache.write('/api/v1/tasks', { scope: 'server-a|account-a', data: [], fetchedAt: 1 });
cache.clear();
assert.equal(cache.read('/api/v1/today', 'server-a|account-a'), null);
assert.equal(cache.read('/api/v1/tasks', 'server-a|account-a'), null);
assert.equal(operations.read('server-a|account-a')[0].operation.op_id, 'one');
assert.equal(storage.getItem('seos.token'), 'bearer');

// Storage is resolved per call, as the bare `localStorage` references were.
let current = new MemoryStorage();
const lazy = new BrowserOfflineOperationStore(() => current);
lazy.write('s', [{ state: 'PENDING' }]);
current = new MemoryStorage();
assert.deepEqual(lazy.read('s'), []);

// Credentials delegate to the preference bridge and describe their real backing.
const prefs = new Map();
const credentials = new PreferencesCredentialStore(
  async (key) => prefs.get(key) ?? null,
  async (key, value) => { if (value == null) prefs.delete(key); else prefs.set(key, String(value)); },
  () => true,
);
await credentials.set('seos.token', 'abc');
assert.equal(await credentials.get('seos.token'), 'abc');
await credentials.set('seos.token', null);
assert.equal(await credentials.get('seos.token'), null);
assert.equal(credentials.security(), 'CAPACITOR_PREFERENCES');
assert.equal(new PreferencesCredentialStore(undefined, undefined, () => false).security(), 'BROWSER_STORAGE');

console.log('device storage boundaries: ok');
