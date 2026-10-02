// The Android storage path end to end in the web client: Keystore credential migration,
// the native SQLite queue (modelled by FakeSeosStorage with the same contract as
// OfflineQueueDb / SessionCredentials), restart, reconnect, lost responses, account /
// server isolation and logout. The Java side has its own unit and device tests.
import assert from 'node:assert/strict';

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.get(key) ?? null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
  key(index) { return [...this.values.keys()][index] ?? null; }
  get length() { return this.values.size; }
}

// Capacitor Preferences (plain app storage) and the native SeosStorage plugin.
const prefs = new Map();
class FakeSeosStorage {
  constructor() { this.queues = new Map(); this.vault = null; this.meta = new Map(); this.failReplace = 0; this.failImport = 0; this.failVault = false; }

  static ids(items) { return items.map((x) => x.operation.op_id); }

  static parse(json) {
    const items = JSON.parse(json);
    const ids = FakeSeosStorage.ids(items);
    if (ids.some((id) => !id) || new Set(ids).size !== ids.length) throw new Error('QUEUE_WRITE');
    return items;
  }

  async queueLoadAll() { return { scopes: Object.fromEntries([...this.queues].map(([k, v]) => [k, structuredClone(v)])) }; }

  async queueReplace({ scope, items }) {
    if (this.failReplace > 0) { this.failReplace -= 1; throw new Error('QUEUE_WRITE'); }
    this.queues.set(scope, FakeSeosStorage.parse(items));
  }

  async queueImportLegacy({ scope, items }) {
    if (this.failImport > 0) { this.failImport -= 1; throw new Error('QUEUE_IMPORT'); }
    const legacy = FakeSeosStorage.parse(items);
    const current = this.queues.get(scope) || [];
    const present = new Set(FakeSeosStorage.ids(current));
    const added = legacy.filter((x) => !present.has(x.operation.op_id));
    this.queues.set(scope, [...current, ...added]);
    const now = new Set(FakeSeosStorage.ids(this.queues.get(scope)));
    return { added: added.length, verified: legacy.every((x) => now.has(x.operation.op_id)) };
  }

  async metaSet({ key, value }) { this.meta.set(key, value); }

  async credentialMigrate() {
    const legacy = prefs.get('seos.token');
    if (!legacy) return { result: 'NOTHING_TO_MIGRATE' };
    if (this.failVault) return { result: 'SECURE_STORE_UNAVAILABLE' };
    this.vault = legacy;
    prefs.delete('seos.token');
    return { result: 'MIGRATED' };
  }

  // SessionCredentials.token(): migrate first; a failed migration still reads the legacy copy.
  async credentialGet() {
    if ((await this.credentialMigrate()).result === 'SECURE_STORE_UNAVAILABLE') return { value: prefs.get('seos.token') ?? null };
    return { value: prefs.get('seos.user') ? this.vault : null };
  }

  async credentialSet({ value }) {
    prefs.delete('seos.token');  // a new login supersedes a legacy copy, even when the vault fails
    if (this.failVault) throw new Error('CREDENTIAL_WRITE');
    this.vault = value;
  }

  async credentialClear() { this.vault = null; prefs.delete('seos.token'); }
}

const native = new FakeSeosStorage();
const localStorage = new MemoryStorage();
globalThis.localStorage = localStorage;
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };
globalThis.window = {
  addEventListener() {}, dispatchEvent() {}, removeEventListener() {},
  Capacitor: {
    isNativePlatform: () => true,
    Plugins: {
      SeosStorage: native,
      Preferences: {
        get: async ({ key }) => ({ value: prefs.get(key) ?? null }),
        set: async ({ key, value }) => { prefs.set(key, value); },
        remove: async ({ key }) => { prefs.delete(key); },
      },
    },
  },
};

// A server with exactly-once replay by op_id.
let online = false;
let loseNextResponse = false;
const applied = new Map();
const received = [];
globalThis.fetch = async (url, options) => {
  if (!online) throw new TypeError('offline');
  if (!String(url).endsWith('/api/v1/sync')) return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({}) };
  const body = JSON.parse(options.body);
  const results = body.operations.map((op) => {
    received.push(op.op_id);
    const replayed = applied.has(op.op_id);
    if (!replayed) applied.set(op.op_id, op);
    return { op_id: op.op_id, status: replayed ? 'NOOP' : 'APPLIED', replayed };
  });
  if (loseNextResponse) { loseNextResponse = false; throw new TypeError('connection reset'); }
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({ results, server_revision: 1 }) };
};

// --- an existing install: plain token + a pending legacy queue ---------------------------
const SERVER = 'https://plan.example';
const scopeA = `${SERVER}|acct-a`;
prefs.set('seos.server', SERVER);
prefs.set('seos.user', JSON.stringify({ account_id: 'acct-a' }));
prefs.set('seos.token', 'bearer-a');
localStorage.setItem(`seos.ops.${scopeA}`, JSON.stringify([
  { operation: { op_id: 'op-legacy-1', type: 'task.start', entity_id: 't1', payload: {} }, state: 'PENDING' },
]));

const storage = await import('../../src/student_execution_os/web/static/js/device-storage.js');
const api = await import('../../src/student_execution_os/web/static/js/api.js');
const sync = await import('../../src/student_execution_os/web/static/js/sync.js');

// An interrupted queue migration leaves the legacy queue in use; nothing is lost.
native.failImport = 1;
const interrupted = await storage.initDeviceStorage();
assert.equal(interrupted.queue, 'FAILED');
assert.equal(interrupted.credentials, 'MIGRATED');
assert.equal(prefs.has('seos.token'), false, 'the plain token is removed after a verified secure write');
await api.restoreSession();
assert.equal(api.session.token, 'bearer-a');
assert.deepEqual(sync.readQueue().map((x) => x.operation.op_id), ['op-legacy-1']);

// The next start completes it; running it again never duplicates.
assert.equal((await storage.initDeviceStorage()).queue, 'VERIFIED');
assert.equal((await storage.initDeviceStorage()).queue, 'VERIFIED');
assert.deepEqual(FakeSeosStorage.ids(native.queues.get(scopeA)), ['op-legacy-1']);
assert.equal(native.meta.get('queue.migrated.v1'), '1');
assert.equal(storage.credentialStore.security(), 'ANDROID_KEYSTORE');

// --- queued offline, committed natively, kept in the legacy mirror -----------------------
const queued = sync.queueOperation('task.complete', 't1', {});
await queued.durable;
assert.deepEqual(FakeSeosStorage.ids(native.queues.get(scopeA)), ['op-legacy-1', queued.op_id]);
assert.deepEqual(JSON.parse(localStorage.getItem(`seos.ops.${scopeA}`)).map((x) => x.operation.op_id),
  ['op-legacy-1', queued.op_id], 'rollback mirror for an older app build');

// --- the app is killed and restarted: a fresh store sees the same queue -------------------
const restarted = new storage.NativeOfflineOperationStore();
await restarted.ready();
assert.deepEqual(restarted.read(scopeA).map((x) => x.operation.op_id), ['op-legacy-1', queued.op_id]);

// --- a failed native commit is never reported as saved -----------------------------------
native.failReplace = 1;
const lost = sync.queueOperation('task.cancel', 't2', {});
await assert.rejects(lost.durable, (error) => error.code === 'LOCAL_STORAGE');
assert.equal(sync.readQueue().some((x) => x.operation.op_id === lost.op_id), false);
await storage.offlineOperationStore.durable().catch(() => {});
assert.equal(FakeSeosStorage.ids(native.queues.get(scopeA)).includes(lost.op_id), false);

// --- reconnect sends once; a lost response is retried without a second mutation ---------
online = true;
loseNextResponse = true;
await sync.flushSync().catch(() => {});
assert.equal(sync.syncState().pending, 2, 'a lost response keeps the operations queued');
await sync.flushSync();
assert.equal(sync.syncState().pending, 0);
assert.deepEqual([...applied.keys()].sort(), ['op-legacy-1', queued.op_id].sort());
assert.equal(received.filter((id) => id === queued.op_id).length, 2, 'resent with the same op_id');
await storage.offlineOperationStore.durable();
assert.ok(native.queues.get(scopeA).every((x) => x.state === 'ACKED'));

// --- account and server isolation --------------------------------------------------------
online = false;
await api.setAuth('bearer-b', { account_id: 'acct-b' });
assert.equal(native.vault, 'bearer-b');
assert.equal(prefs.has('seos.token'), false, 'never written to plain preferences');
assert.equal(sync.syncState().pending, 0, "account B does not see account A's queue");
const forB = sync.queueOperation('task.start', 't9', {});
await forB.durable;
assert.deepEqual(FakeSeosStorage.ids(native.queues.get(`${SERVER}|acct-b`)), [forB.op_id]);
assert.ok(!FakeSeosStorage.ids(native.queues.get(scopeA)).includes(forB.op_id));
await api.setServer('https://other.example');
assert.equal(native.vault, null, 'a server switch drops the credential');
assert.equal(api.session.token, null);

// --- logout clears the credential everywhere ---------------------------------------------
await api.setAuth('bearer-c', { account_id: 'acct-c' });
assert.equal(native.vault, 'bearer-c');
prefs.set('seos.token', 'stale-plain-copy');
await api.clearAuth();
assert.equal(native.vault, null);
assert.equal(prefs.has('seos.token'), false);
assert.equal(prefs.has('seos.user'), false);

// --- the Keystore failing never downgrades the token to plain storage --------------------
native.failVault = true;
await api.setAuth('bearer-d', { account_id: 'acct-d' });
assert.equal(prefs.has('seos.token'), false);
assert.equal(await storage.credentialStore.get('seos.token'), 'bearer-d', 'kept for this run only');
assert.equal(storage.credentialStore.security(), 'MEMORY_ONLY');

// --- a pre-upgrade session whose migration fails is kept, not destroyed ------------------
await api.clearAuth();
prefs.set('seos.user', JSON.stringify({ account_id: 'acct-e' }));
prefs.set('seos.token', 'bearer-legacy-e');
assert.equal((await storage.initDeviceStorage()).credentials, 'SECURE_STORE_UNAVAILABLE');
await api.restoreSession();
assert.equal(api.session.token, 'bearer-legacy-e', 'the existing session keeps working');
assert.equal(prefs.get('seos.token'), 'bearer-legacy-e', 'the legacy copy stays until a migration succeeds');
assert.equal(storage.credentialStore.security(), 'CAPACITOR_PREFERENCES');
// A new login while the Keystore still fails is memory-only and replaces the legacy session.
await api.setAuth('bearer-f', { account_id: 'acct-f' });
assert.equal(prefs.has('seos.token'), false, 'a new credential is never written to plain storage');
assert.equal(await storage.credentialStore.get('seos.token'), 'bearer-f');
assert.equal(storage.credentialStore.security(), 'MEMORY_ONLY');
// After a restart nothing older comes back: the user signs in again.
await storage.initDeviceStorage();
assert.equal(await new storage.DeviceCredentialStore().get('seos.token'), null);

console.log('native storage: ok');
