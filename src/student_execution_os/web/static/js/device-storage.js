import { isNative, prefGet, prefSet } from './native.js';

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
// Current implementations (no storage-format migration has been performed; keys
// are byte-for-byte the ones used before this boundary existed):
//   queue + cache  → WebView/browser localStorage on every platform
//                    (`seos.ops.<scope>`, `seos.lastSync.<scope>`, `seos.cache.<path>`)
//   credentials    → Capacitor Preferences on Android (app-private SharedPreferences,
//                    NOT an OS keystore), localStorage in the browser.
// A native SQLite queue and keystore-backed credentials are follow-up work; any
// such implementation must import the old keys, verify, and only then clear them.

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

  // Honest description of where credentials live; never claims a keystore.
  security() { return this.native() ? 'CAPACITOR_PREFERENCES' : 'BROWSER_STORAGE'; }
}

export const offlineOperationStore = new BrowserOfflineOperationStore();
export const readModelCache = new BrowserReadModelCache();
export const credentialStore = new PreferencesCredentialStore();
