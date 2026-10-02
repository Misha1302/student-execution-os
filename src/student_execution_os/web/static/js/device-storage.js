import { isNative, plugin, prefGet, prefSet } from './native.js';

// Device storage boundaries. Callers depend on three narrow roles instead of on
// localStorage / Capacitor Preferences directly, so a native implementation can
// replace one role without touching sync, cache or session code:
//
//   OfflineOperationStore — the durable outbound operation queue (sync.js). Losing
//                           it loses user work, so writes are allowed to throw.
//   ReadModelCache        — last server responses for offline cold start (store.js).
//                           Disposable: failures degrade to "no cache".
//   CredentialStore       — server URL, bearer token and user (api.js).
//
// Implementations:
//   browser        → localStorage for queue, cache and credentials (keys `seos.ops.<scope>`,
//                    `seos.lastSync.<scope>`, `seos.cache.<path>`, `seos.server|token|user`).
//   Android        → queue: native SQLite (SeosStorage plugin, OfflineQueueDb), committed
//                    before a change is reported as saved; a synchronous in-memory mirror
//                    serves the UI. The legacy localStorage copy is still written as a
//                    rollback mirror (an older app build restored by a newer versionCode
//                    keeps seeing its pending operations; a resend is absorbed by the
//                    server's op_id exactly-once replay).
//                    token: Android Keystore (AES-GCM, non-exportable key) via SeosStorage;
//                    it is never written to plain Preferences again. Server URL and user
//                    are not secrets and stay in Capacitor Preferences.
//                    cache: WebView localStorage (disposable by design).
// Migration (ready()): legacy token → Keystore (verified, then the plain copy removed);
// every legacy queue → native, idempotent by op_id, verified before it is marked done.

// Resolved per call, like the bare `localStorage` references this replaced, so a
// storage that appears or is swapped after import is honoured.
const browserStorage = () => globalThis.localStorage;
const resolver = (storage) => (typeof storage === 'function' ? storage : () => storage);

export class BrowserOfflineOperationStore {
  constructor(storage = browserStorage) { this.storage = resolver(storage); }

  read(scope) {
    try {
      const value = JSON.parse(this.storage().getItem(`seos.ops.${scope}`) || '[]');
      return Array.isArray(value) ? value : [];
    } catch { return []; }
  }

  write(scope, items) { this.storage().setItem(`seos.ops.${scope}`, JSON.stringify(items)); }

  lastSyncedAt(scope) {
    try { return Number(this.storage().getItem(`seos.lastSync.${scope}`)) || null; } catch { return null; }
  }

  setLastSyncedAt(scope, value) { this.storage().setItem(`seos.lastSync.${scope}`, String(value)); }
}

export class BrowserReadModelCache {
  constructor(storage = browserStorage) { this.storage = resolver(storage); }

  read(path, scope) {
    try {
      const value = JSON.parse(this.storage().getItem(`seos.cache.${path}`) || 'null');
      return value && value.scope === scope ? value : null;
    } catch { return null; }
  }

  write(path, entry) { this.storage().setItem(`seos.cache.${path}`, JSON.stringify(entry)); }

  // Only cache keys: the operation queue and credentials survive a cache wipe.
  clear() {
    const storage = this.storage();
    const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index))
      .filter((key) => key?.startsWith('seos.cache.'));
    keys.forEach((key) => storage.removeItem(key));
  }
}

export class PreferencesCredentialStore {
  constructor(get = prefGet, set = prefSet, native = isNative) {
    this.get = get;
    this.set = set;
    this.native = native;
  }

  async ready() { return null; }

  // Honest description of where credentials live; never claims a keystore.
  security() { return this.native() ? 'CAPACITOR_PREFERENCES' : 'BROWSER_STORAGE'; }
}

const TOKEN_KEY = 'seos.token';
const ARGS = (scope, items) => ({ scope, items: JSON.stringify(items) });

export class NativeOfflineOperationStore {
  constructor(native = () => plugin('SeosStorage'), legacy = new BrowserOfflineOperationStore(),
    storage = browserStorage) {
    this.native = native;
    this.legacy = legacy;
    this.storage = resolver(storage);
    this.mirror = null; // Map<scope, items> once the native queue is loaded
    this.chain = Promise.resolve();
    this.migration = 'NOT_STARTED';
  }

  get nativeActive() { return Boolean(this.mirror && this.native()); }

  legacyScopes() {
    try {
      const storage = this.storage();
      return Array.from({ length: storage.length }, (_, index) => storage.key(index))
        .filter((key) => key?.startsWith('seos.ops.')).map((key) => key.slice('seos.ops.'.length));
    } catch { return []; }
  }

  // Loads the native queue and imports every legacy queue into it (by op_id, so a
  // re-run or an interrupted run never duplicates). Until this resolves the legacy
  // store is used, so nothing is invisible during start-up.
  async ready() {
    const store = this.native();
    if (!store) { this.migration = 'NOT_NATIVE'; return this.migration; }
    let verified = true;
    for (const scope of this.legacyScopes()) {
      const items = this.legacy.read(scope);
      if (!items.length) continue;
      const result = await store.queueImportLegacy(ARGS(scope, items));
      verified = verified && Boolean(result?.verified);
    }
    const { scopes } = await store.queueLoadAll();
    this.mirror = new Map(Object.entries(scopes || {}));
    // An unverified import keeps every legacy item visible (union by op_id).
    if (!verified) {
      for (const scope of this.legacyScopes()) {
        const known = new Set((this.mirror.get(scope) || []).map((x) => x.operation?.op_id));
        const extra = this.legacy.read(scope).filter((x) => !known.has(x.operation?.op_id));
        if (extra.length) this.mirror.set(scope, [...(this.mirror.get(scope) || []), ...extra]);
      }
    }
    this.migration = verified ? 'VERIFIED' : 'INCOMPLETE';
    if (verified) await store.metaSet({ key: 'queue.migrated.v1', value: '1' }).catch(() => {});
    return this.migration;
  }

  read(scope) {
    if (!this.nativeActive) return this.legacy.read(scope);
    return [...(this.mirror.get(scope) || [])];
  }

  write(scope, items) {
    if (!this.nativeActive) { this.legacy.write(scope, items); return; }
    this.mirror.set(scope, [...items]);
    try { this.legacy.write(scope, items); } catch { /* rollback mirror only; native is authoritative */ }
    const store = this.native();
    const next = this.chain.catch(() => {}).then(() => store.queueReplace(ARGS(scope, items)));
    this.chain = next;
  }

  // Resolves when every write so far is committed to the native database.
  durable() { return this.nativeActive ? this.chain : Promise.resolve(); }

  lastSyncedAt(scope) { return this.legacy.lastSyncedAt(scope); }

  setLastSyncedAt(scope, value) { this.legacy.setLastSyncedAt(scope, value); }
}

export class DeviceCredentialStore {
  constructor(native = () => plugin('SeosStorage'), get = prefGet, set = prefSet, isApp = isNative) {
    this.native = native;
    this.prefGet = get;
    this.prefSet = set;
    this.isApp = isApp;
    this.memory = undefined; // the token, only when the secure store failed
    this.state = null;
  }

  // Moves a legacy plain token into the Keystore (native side: write, verify, then remove).
  async ready() {
    const store = this.native();
    if (!store) return null;
    try {
      const { result } = await store.credentialMigrate();
      this.state = result === 'SECURE_STORE_UNAVAILABLE' || result === 'VERIFY_FAILED' ? 'LEGACY' : 'KEYSTORE';
      return result;
    } catch {
      this.state = 'LEGACY';
      return 'FAILED';
    }
  }

  async get(key) {
    const store = this.native();
    if (key !== TOKEN_KEY || !store) return this.prefGet(key);
    if (this.memory !== undefined) return this.memory;
    try {
      const { value } = await store.credentialGet();
      return value ?? null;
    } catch { return null; }
  }

  async set(key, value) {
    const store = this.native();
    if (key !== TOKEN_KEY || !store) { await this.prefSet(key, value); return; }
    try {
      if (value == null) await store.credentialClear();
      else await store.credentialSet({ value: String(value) });
      this.memory = undefined;
      if (value != null) this.state = 'KEYSTORE';
    } catch {
      // Never downgrade to plain storage: keep the session for this run only.
      this.memory = value == null ? undefined : String(value);
      this.state = value == null ? this.state : 'MEMORY_ONLY';
      if (value == null) await store.credentialClear().catch(() => {});
    }
  }

  security() {
    if (!this.isApp()) return 'BROWSER_STORAGE';
    if (!this.native()) return 'CAPACITOR_PREFERENCES';
    return this.state === 'MEMORY_ONLY' ? 'MEMORY_ONLY' : this.state === 'LEGACY' ? 'CAPACITOR_PREFERENCES' : 'ANDROID_KEYSTORE';
  }
}

export const offlineOperationStore = new NativeOfflineOperationStore();
export const readModelCache = new BrowserReadModelCache();
export const credentialStore = new DeviceCredentialStore();

// Called once at start-up, before the session and the queue are read.
export async function initDeviceStorage() {
  const results = {};
  try { results.credentials = await credentialStore.ready(); } catch { results.credentials = 'FAILED'; }
  try { results.queue = await offlineOperationStore.ready(); } catch { results.queue = 'FAILED'; }
  return results;
}
