import { prefGet, prefSet } from './native.js';
import { AndroidUpdateAdapter } from './update-android.js';
import {
  MandatoryMode, SemVer, UpdateChannel, UpdateError, UpdatePolicy, UpdateState,
  selectExactTarget, transition,
} from './update-domain.js';

const STORE_KEY = 'seos.update.state.v1';
const SIX_HOURS = 6 * 60 * 60 * 1000;
const POLICY_CACHE_MS = 15 * 60 * 1000;
const LONG_OFFLINE_MS = 30 * 60 * 1000;

function newInstallationId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16); globalThis.crypto?.getRandomValues?.(bytes);
  if (!bytes.some(Boolean)) throw new UpdateError('RANDOM_UNAVAILABLE', 'Secure random generator is unavailable');
  bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

class UpdateStateStore {
  constructor() { this.data = null; }

  async load() {
    if (this.data) return this.data;
    let saved = null;
    try { saved = JSON.parse((await prefGet(STORE_KEY)) || 'null'); } catch { /* corrupted state is replaced, never trusted */ }
    this.data = {
      installationId: typeof saved?.installationId === 'string' ? saved.installationId : newInstallationId(),
      channel: saved?.channel === UpdateChannel.BETA ? UpdateChannel.BETA : UpdateChannel.STABLE,
      autoCheck: saved?.autoCheck !== false,
      autoDownload: saved?.autoDownload !== false,
      sequences: saved?.sequences && typeof saved.sequences === 'object' ? saved.sequences : {},
      lastCheckAt: Number(saved?.lastCheckAt) || 0,
      pending: saved?.pending || null,
      pendingDownload: saved?.pendingDownload || null,
      health: saved?.health || null,
    };
    await this.save();
    return this.data;
  }

  async save() { await prefSet(STORE_KEY, JSON.stringify(this.data)); }
}

class StaticUpdatePolicyProvider {
  constructor(adapter, store, configuration) {
    this.adapter = adapter; this.store = store; this.configuration = configuration; this.cache = new Map();
  }

  source(channel) {
    const template = String(this.configuration.policyUrlTemplate || '');
    if (!template.includes('{channel}')) throw new UpdateError('UPDATER_UNAVAILABLE', 'Update policy URL is not configured');
    let url;
    try { url = new URL(template.replace('{channel}', channel.toLowerCase())); }
    catch (error) { throw new UpdateError('METADATA_INVALID', 'Update policy URL is invalid', { cause: error }); }
    if (url.username || url.password || url.hash) throw new UpdateError('METADATA_INVALID', 'Update policy URL cannot contain credentials or a fragment');
    if (!this.configuration.debug && url.protocol !== 'https:') throw new UpdateError('METADATA_INVALID', 'Production update policy must use HTTPS');
    const localHttp = this.configuration.debug && url.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '10.0.2.2'].includes(url.hostname);
    if (url.protocol !== 'https:' && !localHttp) throw new UpdateError('METADATA_INVALID', 'Unsupported update policy protocol');
    return url.toString();
  }

  async getPolicy(channel, { bypassCache = false } = {}) {
    const source = this.source(channel);
    const cached = this.cache.get(source);
    if (!bypassCache && cached && Date.now() - cached.fetchedAt < POLICY_CACHE_MS) return cached.policy;
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 15000);
    let response;
    try { response = await fetch(source, { signal: controller.signal, redirect: 'follow', cache: bypassCache ? 'no-store' : 'default' }); }
    catch (error) { throw new UpdateError('METADATA_UNAVAILABLE', error?.name === 'AbortError' ? 'Update check timed out' : 'Update metadata unavailable', { retryable: true, cause: error }); }
    finally { clearTimeout(timer); }
    if (!response.ok) throw new UpdateError('METADATA_UNAVAILABLE', `Update metadata HTTP ${response.status}`, { retryable: response.status >= 500 });
    if (!this.configuration.debug && new URL(response.url || source).protocol !== 'https:') throw new UpdateError('METADATA_INVALID', 'Update metadata redirected away from HTTPS');
    const length = Number(response.headers.get('content-length') || 0);
    if (length > 1024 * 1024) throw new UpdateError('METADATA_INVALID', 'Update metadata is too large');
    const text = await response.text();
    if (text.length > 1024 * 1024) throw new UpdateError('METADATA_INVALID', 'Update metadata is too large');
    let raw;
    try { raw = JSON.parse(text); } catch (error) { throw new UpdateError('METADATA_INVALID', 'Update metadata is not JSON', { cause: error }); }
    await this.adapter.verifyPolicy(raw); // authentication precedes every policy decision
    const policy = new UpdatePolicy(raw);
    if (!this.configuration.debug && policy.releases.some((release) => release.artifacts.some((artifact) => !artifact.url.startsWith('https://')))) {
      throw new UpdateError('METADATA_INVALID', 'Production artifacts must use HTTPS');
    }
    if (policy.channel !== channel) throw new UpdateError('METADATA_INVALID', 'Wrong update channel');
    if (policy.expiresAt <= new Date()) throw new UpdateError('METADATA_EXPIRED', 'Update metadata has expired');
    const state = await this.store.load();
    const last = Number(state.sequences[source] || 0);
    if (policy.sequence < last) throw new UpdateError('METADATA_ROLLBACK', 'Older update metadata was rejected');
    state.sequences[source] = Math.max(last, policy.sequence);
    await this.store.save();
    this.cache.set(source, { fetchedAt: Date.now(), policy });
    return policy;
  }
}

export class AppUpdateService {
  constructor(adapter = new AndroidUpdateAdapter()) {
    this.adapter = adapter;
    this.store = new UpdateStateStore();
    this.configuration = null;
    this.provider = null;
    this.current = {
      status: UpdateState.IDLE, enabled: false, currentVersion: null, target: null,
      progress: 0, error: null, downloaded: null, lastCheckAt: null,
    };
    this.operation = null;
    this.listeners = new Set();
    this.periodic = null;
    this.offlineAt = null;
    this.initializing = null;
  }

  subscribe(listener) { this.listeners.add(listener); listener(this.getState()); return () => this.listeners.delete(listener); }
  getState() { return { ...this.current }; }
  getCurrentVersion() { return this.current.currentVersion; }

  emit() {
    const state = this.getState();
    this.listeners.forEach((listener) => listener(state));
    globalThis.window?.dispatchEvent?.(new CustomEvent('seos-update-state', { detail: state }));
  }

  setStatus(next, patch = {}) {
    const previous = this.current.status;
    this.current.status = transition(this.current.status, next);
    Object.assign(this.current, patch);
    console.info('seos.update', JSON.stringify({
      from: previous, to: next, current_version: this.current.currentVersion,
      target_version: this.current.target?.release?.version?.toString?.() || null,
      channel: this.current.target?.release?.channel || null,
      platform: this.configuration?.platform || null, architecture: this.configuration?.architecture || null,
      failure_code: this.current.error?.code || null,
    }));
    this.emit();
  }

  async initialize() {
    if (this.initializing) return this.initializing;
    this.initializing = this.initializeOnce();
    return this.initializing;
  }

  async initializeOnce() {
    const saved = await this.store.load();
    this.configuration = await this.adapter.configuration();
    this.current.enabled = Boolean(this.configuration.enabled && this.adapter.available);
    this.current.currentVersion = this.configuration.versionName || null;
    this.current.lastCheckAt = saved.lastCheckAt ? new Date(saved.lastCheckAt) : null;
    this.provider = this.current.enabled ? new StaticUpdatePolicyProvider(this.adapter, this.store, this.configuration) : null;
    if (this.current.enabled && saved.pendingDownload?.policy && saved.pendingDownload?.downloaded) {
      try {
        await this.adapter.verifyPolicy(saved.pendingDownload.policy);
        const policy = new UpdatePolicy(saved.pendingDownload.policy);
        const target = selectExactTarget(policy, {
          currentVersion: this.current.currentVersion, platform: this.configuration.platform,
          architecture: this.configuration.architecture, sdk: this.configuration.sdk,
          installationId: saved.installationId,
        });
        if (target && target.release.releaseId === saved.pendingDownload.releaseId
            && target.artifact.sha256 === saved.pendingDownload.sha256) {
          await this.adapter.verifyDownloaded(target, saved.pendingDownload.downloaded);
          this.setStatus(UpdateState.READY_TO_INSTALL, { target, downloaded: saved.pendingDownload.downloaded, progress: 1 });
        } else {
          saved.pendingDownload = null; await this.store.save();
        }
      } catch {
        saved.pendingDownload = null; await this.store.save();
      }
    }
    if (saved.health?.pendingVersion && saved.health.pendingVersion === this.current.currentVersion) {
      saved.health.launchAttempts = Number(saved.health.launchAttempts || 0) + 1;
      saved.health.startupHealth = 'pending';
      await this.store.save();
      if (saved.health.launchAttempts >= 3) {
        this.current.error = new UpdateError('STARTUP_UNHEALTHY', 'The updated version did not complete startup several times');
      }
    }
    this.emit();
    return this.getState();
  }

  async markHealthy() {
    const state = await this.store.load();
    if (state.health?.pendingVersion === this.current.currentVersion) {
      state.health = { ...state.health, startupHealth: 'healthy', healthyAt: new Date().toISOString() };
      state.pending = null;
      state.pendingDownload = null;
      await this.store.save();
    }
  }

  async reconcileInstallerState() {
    if (!this.current.enabled || typeof this.adapter.nativeState !== 'function') return this.getState();
    let native;
    try { native = await this.adapter.nativeState(); } catch { return this.getState(); }
    if (native?.state !== 'FAILED') return this.getState();
    const saved = await this.store.load();
    if (!saved.pending?.version || native.targetVersion !== saved.pending.version
        || Number(native.targetBuild) !== Number(saved.pending.buildNumber)) return this.getState();
    saved.pending = null;
    saved.health = null;
    await this.store.save();
    const error = new UpdateError(String(native.code || 'INSTALLER_FAILED'), String(native.message || native.code || 'Android could not install the update'), {
      retryable: ['INSTALL_CANCELLED', 'PERMISSION_REQUIRED', 'INSUFFICIENT_DISK'].includes(native.code),
    });
    if ([UpdateState.READY_TO_INSTALL, UpdateState.APPLYING, UpdateState.RESTART_REQUIRED].includes(this.current.status)) {
      this.setStatus(UpdateState.FAILED, { error });
    }
    return this.getState();
  }

  async preferences() {
    const state = await this.store.load();
    return { channel: state.channel, autoCheck: state.autoCheck, autoDownload: state.autoDownload,
      betaChannelAvailable: Boolean(this.configuration?.betaChannelAvailable) };
  }

  async setPreferences(patch) {
    const state = await this.store.load();
    if (patch.channel != null) {
      if (!VALUES_CHANNEL.has(patch.channel) || (patch.channel === UpdateChannel.BETA && !this.configuration?.betaChannelAvailable)) throw new UpdateError('INVALID_PREFERENCE', 'Channel is unavailable');
      state.channel = patch.channel;
    }
    if (patch.autoCheck != null) state.autoCheck = Boolean(patch.autoCheck);
    if (patch.autoDownload != null) state.autoDownload = Boolean(patch.autoDownload);
    await this.store.save(); this.emit();
  }

  async withOperation(fn) {
    if (this.operation) throw new UpdateError('UPDATE_LOCKED', 'Another update operation is already running', { retryable: true });
    this.operation = Promise.resolve().then(fn);
    try { return await this.operation; } finally { this.operation = null; }
  }

  async checkForUpdates({ manual = false } = {}) {
    if (!this.current.enabled) return null;
    return this.withOperation(async () => {
      const readyTarget = this.current.status === UpdateState.READY_TO_INSTALL ? this.current.target : null;
      const readyDownload = this.current.status === UpdateState.READY_TO_INSTALL ? this.current.downloaded : null;
      this.setStatus(UpdateState.CHECKING, { error: null, progress: 0 });
      try {
        const saved = await this.store.load();
        const policy = await this.provider.getPolicy(saved.channel, { bypassCache: manual });
        const target = selectExactTarget(policy, {
          currentVersion: this.current.currentVersion, platform: this.configuration.platform,
          architecture: this.configuration.architecture, sdk: this.configuration.sdk,
          installationId: saved.installationId,
        });
        saved.lastCheckAt = Date.now(); await this.store.save();
        if (!target) {
          saved.pendingDownload = null; await this.store.save();
          this.setStatus(UpdateState.UP_TO_DATE, { target: null, downloaded: null, lastCheckAt: new Date(saved.lastCheckAt) });
          return null;
        }
        if (readyTarget?.release.releaseId === target.release.releaseId && readyTarget.artifact.sha256 === target.artifact.sha256 && readyDownload) {
          this.setStatus(UpdateState.READY_TO_INSTALL, { target, downloaded: readyDownload, progress: 1, lastCheckAt: new Date(saved.lastCheckAt) });
          return target;
        }
        if (saved.pendingDownload?.releaseId !== target.release.releaseId || saved.pendingDownload?.sha256 !== target.artifact.sha256) {
          saved.pendingDownload = null; await this.store.save();
        }
        this.setStatus(UpdateState.AVAILABLE, { target, downloaded: null, lastCheckAt: new Date(saved.lastCheckAt) });
        if (!manual && saved.autoDownload && target.release.channel === UpdateChannel.STABLE) setTimeout(() => this.download(target).catch(() => {}), 0);
        return target;
      } catch (error) {
        const typed = error instanceof UpdateError ? error : new UpdateError('UNKNOWN', String(error?.message || error), { cause: error });
        this.setStatus(UpdateState.FAILED, { error: typed });
        throw typed;
      }
    });
  }

  async download(target = this.current.target) {
    if (!target) throw new UpdateError('INVALID_STATE', 'No selected update target');
    return this.withOperation(async () => {
      if (![UpdateState.AVAILABLE, UpdateState.FAILED].includes(this.current.status)) throw new UpdateError('INVALID_STATE', 'Update cannot be downloaded now');
      this.setStatus(UpdateState.DOWNLOADING, { target, error: null, progress: 0 });
      let handle = null;
      try {
        handle = await this.adapter.onProgress((event) => {
          if (this.current.status === UpdateState.DOWNLOADING) { this.current.progress = Math.max(0, Math.min(1, Number(event.fraction) || 0)); this.emit(); }
        });
        const downloaded = await this.adapter.download(target);
        this.setStatus(UpdateState.DOWNLOADED, { downloaded, progress: 1 });
        await this.adapter.verifyDownloaded(target, downloaded);
        const saved = await this.store.load();
        saved.pendingDownload = {
          policy: target.policy.raw, releaseId: target.release.releaseId,
          sha256: target.artifact.sha256, downloaded,
        };
        await this.store.save();
        this.setStatus(UpdateState.READY_TO_INSTALL);
        return downloaded;
      } catch (error) {
        const typed = error instanceof UpdateError ? error : new UpdateError('UNKNOWN', String(error?.message || error));
        this.setStatus(UpdateState.FAILED, { error: typed });
        throw typed;
      } finally { await handle?.remove?.(); }
    });
  }

  async apply(target = this.current.target) {
    if (!target || !this.current.downloaded) throw new UpdateError('INVALID_STATE', 'No verified update is ready');
    return this.withOperation(async () => {
      try {
        // A fresh signed policy is mandatory here: a downloaded release may have
        // been paused or withdrawn since discovery.
        const saved = await this.store.load();
        const policy = await this.provider.getPolicy(saved.channel, { bypassCache: true });
        const fresh = selectExactTarget(policy, {
          currentVersion: this.current.currentVersion, platform: this.configuration.platform,
          architecture: this.configuration.architecture, sdk: this.configuration.sdk,
          installationId: saved.installationId,
        });
        if (!fresh || fresh.release.releaseId !== target.release.releaseId || fresh.artifact.sha256 !== target.artifact.sha256) {
          throw new UpdateError('RELEASE_NO_LONGER_AVAILABLE', 'The release was paused, withdrawn, or replaced');
        }
        await this.adapter.verifyDownloaded(fresh, this.current.downloaded);
        this.setStatus(UpdateState.APPLYING, { target: fresh, error: null });
        saved.pending = { version: fresh.release.version.toString(), buildNumber: fresh.release.buildNumber, sha256: fresh.artifact.sha256 };
        saved.health = { pendingVersion: fresh.release.version.toString(), launchAttempts: 0, startupHealth: 'pending', appliedAt: new Date().toISOString(), rollbackCompatibility: fresh.release.rollbackCompatibility };
        await this.store.save();
        const result = await this.adapter.apply(fresh, this.current.downloaded);
        this.setStatus(UpdateState.RESTART_REQUIRED);
        return result;
      } catch (error) {
        const typed = error instanceof UpdateError ? error : new UpdateError('INSTALLER_FAILED', String(error?.message || error));
        if (this.current.status === UpdateState.APPLYING) {
          const saved = await this.store.load();
          saved.pending = null; saved.health = null; await this.store.save();
        }
        if (typed.code === 'RELEASE_NO_LONGER_AVAILABLE') {
          const saved = await this.store.load(); saved.pendingDownload = null; await this.store.save();
          this.current.downloaded = null;
        }
        if (this.current.status === UpdateState.READY_TO_INSTALL || this.current.status === UpdateState.APPLYING) this.setStatus(UpdateState.FAILED, { error: typed });
        throw typed;
      }
    });
  }

  async applyAndRestart(target = this.current.target) { return this.apply(target); }

  async startBackgroundChecks() {
    if (!this.current.enabled) return;
    const saved = await this.store.load();
    if (saved.autoCheck) setTimeout(() => this.checkForUpdates().catch(() => {}), 0);
    const jitter = ((saved.installationId.charCodeAt(0) || 0) % 31 - 15) * 60 * 1000;
    this.periodic = setInterval(async () => {
      const preferences = await this.store.load();
      if (preferences.autoCheck && Date.now() - preferences.lastCheckAt >= SIX_HOURS + jitter) this.checkForUpdates().catch(() => {});
    }, 15 * 60 * 1000);
    window.addEventListener('offline', () => { this.offlineAt = Date.now(); });
    window.addEventListener('online', () => {
      if (this.offlineAt && Date.now() - this.offlineAt >= LONG_OFFLINE_MS) this.checkForUpdates().catch(() => {});
      this.offlineAt = null;
    });
  }
}

const VALUES_CHANNEL = new Set(Object.values(UpdateChannel));
export const appUpdateService = new AppUpdateService();

export async function startUpdateRuntime() {
  try {
    const state = await appUpdateService.initialize();
    if (!state.enabled) return state;
    await appUpdateService.reconcileInstallerState();
    // Reaching this point means the main shell rendered and critical startup
    // completed; late runtime crashes are not updater failures.
    await appUpdateService.markHealthy();
    await appUpdateService.startBackgroundChecks();
    return appUpdateService.getState();
  } catch (error) {
    console.warn('update runtime unavailable', error?.code || error?.message);
    return appUpdateService.getState();
  }
}

export { MandatoryMode, SemVer, UpdateChannel, UpdateError, UpdateState };
