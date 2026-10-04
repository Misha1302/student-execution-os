import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

console.info = () => {}; // operational update logs are asserted structurally elsewhere

globalThis.window = { Capacitor: null, addEventListener() {}, dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };
const values = new Map();
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: (key) => values.delete(key),
};

const domain = await import('../../src/student_execution_os/web/static/js/update-domain.js');
const { AppUpdateService } = await import('../../src/student_execution_os/web/static/js/update-service.js');
const compatibility = await import('../../src/student_execution_os/web/static/js/update-compatibility.js');
const en = (await import('../../src/student_execution_os/web/static/js/i18n/en.js')).default;
const ru = (await import('../../src/student_execution_os/web/static/js/i18n/ru.js')).default;
const { SemVer, UpdateChannel, UpdateError, UpdatePolicy, UpdateState, evaluateCompatibility, rolloutBucket, selectExactTarget, transition } = domain;
let assertions = 0;
const ok = (condition, message) => { assertions += 1; assert.ok(condition, message); };
const equal = (left, right, message) => { assertions += 1; assert.equal(left, right, message); };

const ordered = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0'];
for (let i = 1; i < ordered.length; i += 1) ok(new SemVer(ordered[i - 1]).compare(ordered[i]) < 0, 'SemVer ordering');
equal(new SemVer('1.0.0+one').compare('1.0.0+two'), 0, 'build metadata precedence');
const corpus = JSON.parse(readFileSync(new URL('../fixtures/update_protocol_corpus.json', import.meta.url), 'utf8'));
for (let i = 1; i < corpus.semver_order.length; i += 1) ok(new SemVer(corpus.semver_order[i - 1]).compare(corpus.semver_order[i]) < 0, 'shared SemVer ordering corpus');
for (const [left, right] of corpus.semver_equal_precedence) equal(new SemVer(left).compare(right), 0, 'shared SemVer equal-precedence corpus');
for (const value of corpus.semver_invalid) { assert.throws(() => new SemVer(value)); assertions += 1; }
for (const code of domain.PUBLIC_UPDATE_ERRORS) {
  const key = `updates.error.${code}`;
  ok(typeof en[key] === 'string' && en[key] !== key, `English translation for ${code}`);
  ok(typeof ru[key] === 'string' && ru[key] !== key, `Russian translation for ${code}`);
}
compatibility.setClientCompatibility('UNSUPPORTED');
equal(compatibility.onlineOperationAllowed('/api/v1/sync'), false, 'unsupported client blocks sync');
equal(compatibility.onlineOperationAllowed('/api/v1/account/export'), true, 'unsupported client preserves export');
equal(compatibility.onlineOperationAllowed('/api/v1/health'), true, 'unsupported client preserves recovery health');
compatibility.setClientCompatibility('SUPPORTED');

function rawPolicy({ version = '1.0.1', build = 101, channel = 'STABLE', status = 'AVAILABLE', rollout = 100,
  sequence = 1, expires = '2099-01-02T00:00:00Z', mandatory = { mode: 'OPTIONAL' }, minVersion = '0.9.0', hash = 'a'.repeat(64) } = {}) {
  return {
    schema_version: 1, sequence, generated_at: '2026-09-26T00:00:00Z', expires_at: expires,
    channel, latest_version: version, minimum_supported_version: minVersion, rollout: { percentage: rollout },
    releases: [{
      version, build_number: build, channel, published_at: '2026-09-26T00:00:00Z', status,
      severity: 'NORMAL', release_notes: { en: { summary: 'Update', changes: ['Safe'] }, ru: { summary: 'Обновление', changes: ['Безопасно'] } },
      minimum_os_versions: { android_sdk: 24 }, mandatory_policy: mandatory,
      rollback_compatibility: 'BINARY_ONLY', artifacts: [{ platform: 'android', architecture: 'universal', artifact_kind: 'APK',
        url: 'https://updates.example/app.apk', size_bytes: 4, sha256: hash,
        updater_metadata: { package_name: 'io.github.misha1302.seos', version_code: build, min_sdk: 24 } }],
    }], signature_metadata: { key_id: 'test-key', algorithm: 'Ed25519', signature: 'signed' },
  };
}

const targetArgs = { currentVersion: '1.0.0', currentVersionCode: 100, platform: 'android', architecture: 'arm64', sdk: 35, installationId: 'install-a', now: new Date('2026-09-26T12:00:00Z') };
let parsed = new UpdatePolicy(rawPolicy());
equal(selectExactTarget(parsed, targetArgs).release.version.toString(), '1.0.1', 'newer exact target');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ version: '0.9.9' })), targetArgs), null, 'older release not selected');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ version: '1.0.0', build: 101 })), targetArgs), null, 'same SemVer binary revision is forbidden');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ version: '1.0.1', build: 100 })), targetArgs), null, 'newer SemVer with same versionCode is rejected');
ok(selectExactTarget(new UpdatePolicy(rawPolicy({ version: '1.0.1', build: 102 })), targetArgs), 'newer SemVer and higher versionCode accepted');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ status: 'PAUSED' })), targetArgs), null, 'paused release');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ status: 'WITHDRAWN' })), targetArgs), null, 'withdrawn release');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ rollout: 0 })), targetArgs), null, 'rollout zero');
ok(selectExactTarget(new UpdatePolicy(rawPolicy({ rollout: 100 })), targetArgs), 'rollout one hundred');
equal(rolloutBucket('install-a', parsed.latestRelease.releaseId), rolloutBucket('install-a', parsed.latestRelease.releaseId), 'stable bucket');
equal(selectExactTarget(parsed, { ...targetArgs, platform: 'linux' }), null, 'wrong platform ignored');
equal(selectExactTarget(parsed, { ...targetArgs, sdk: 23 }), null, 'old OS ignored');
equal(evaluateCompatibility(new UpdatePolicy(rawPolicy({ minVersion: '1.0.1' })), targetArgs), 'UNSUPPORTED', 'minimum version is enforced independently of artifact compatibility');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ minVersion: '1.0.1' })), targetArgs).mandatory, 'UNSUPPORTED_CLIENT', 'minimum version mandatory');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ minVersion: '1.0.1', rollout: 0 })), targetArgs).mandatory, 'UNSUPPORTED_CLIENT', 'unsupported client bypasses optional rollout');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ mandatory: { mode: 'REQUIRED_AFTER', required_after: '2026-09-27T00:00:00Z' } })), targetArgs).mandatory, 'OPTIONAL', 'mandatory deadline not reached');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ mandatory: { mode: 'REQUIRED_AFTER', required_after: '2026-09-27T00:00:00Z' } })), targetArgs).compatibility, 'UPDATE_REQUIRED_SOON', 'mandatory deadline warning state');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ mandatory: { mode: 'REQUIRED_AFTER', required_after: '2026-09-26T01:00:00Z' } })), targetArgs).mandatory, 'REQUIRED_AFTER', 'mandatory deadline reached');
equal(selectExactTarget(new UpdatePolicy(rawPolicy({ mandatory: { mode: 'REQUIRED_AFTER', required_after: '2026-09-26T01:00:00Z' } })), targetArgs).compatibility, 'UNSUPPORTED', 'mandatory deadline blocks unsafe online protocol');
assert.throws(() => new UpdatePolicy(rawPolicy({ channel: 'STABLE', version: '1.0.1-beta.1' })), /Stable/i); assertions += 1;
ok(new UpdatePolicy(rawPolicy({ channel: 'BETA', version: '1.0.1-beta.1' })), 'beta prerelease accepted');
equal(transition('IDLE', 'CHECKING'), 'CHECKING', 'valid state transition');
assert.throws(() => transition('IDLE', 'APPLYING'), /Invalid update transition/); assertions += 1;

class FakeAdapter {
  constructor() { this.available = true; this.signatureValid = true; this.downloadFailure = null; this.applyCalls = 0; this.verifyCalls = 0; this.installerState = { state: 'IDLE' }; }
  async configuration() { return { enabled: true, debug: false, policyUrlTemplate: 'https://updates.example/updates-{channel}.json', betaChannelAvailable: true,
    platform: 'android', architecture: 'arm64', sdk: 35, versionName: '1.0.0', buildNumber: 100 }; }
  async verifyPolicy() { if (!this.signatureValid) throw new UpdateError('SIGNATURE_INVALID', 'bad signature'); }
  async onProgress() { return { remove() {} }; }
  async download() { if (this.downloadFailure) throw this.downloadFailure; return { path: '/cache/update.apk', sizeBytes: 4 }; }
  async verifyDownloaded() { this.verifyCalls += 1; return { verified: true }; }
  async apply() { this.applyCalls += 1; return { status: 'COMMITTED' }; }
  async nativeState() { return this.installerState; }
}

let served = rawPolicy(); let fetches = 0; let holdFetch = null;
globalThis.fetch = async () => {
  fetches += 1;
  if (holdFetch) await holdFetch;
  return { ok: true, status: 200, url: 'https://updates.example/policy.json', headers: { get: () => null }, text: async () => JSON.stringify(served) };
};

values.clear();
const adapter = new FakeAdapter(); const service = new AppUpdateService(adapter);
await service.initialize();
await service.checkForUpdates();
equal(service.getState().status, UpdateState.AVAILABLE, 'service discovers target');
equal(fetches, 1, 'first policy fetch');
await service.checkForUpdates();
equal(fetches, 1, 'automatic check uses freshness cache');
await service.checkForUpdates({ manual: true });
equal(fetches, 2, 'manual check bypasses cache');
await service.download();
equal(service.getState().status, UpdateState.READY_TO_INSTALL, 'verified download is ready');
equal(adapter.verifyCalls, 1, 'download verified before ready');
served = rawPolicy({ sequence: 2, status: 'PAUSED' });
await assert.rejects(service.apply(), (error) => error.code === 'RELEASE_NO_LONGER_AVAILABLE'); assertions += 1;
equal(adapter.applyCalls, 0, 'paused release never reaches adapter apply');

values.clear(); served = rawPolicy({ sequence: 3 }); fetches = 0;
const interruptedAdapter = new FakeAdapter(); interruptedAdapter.downloadFailure = new UpdateError('DOWNLOAD_INTERRUPTED', 'cut', { retryable: true });
const interrupted = new AppUpdateService(interruptedAdapter); await interrupted.initialize(); await interrupted.checkForUpdates();
await assert.rejects(interrupted.download(), (error) => error.code === 'DOWNLOAD_INTERRUPTED'); assertions += 1;
equal(interrupted.getState().status, UpdateState.FAILED, 'interrupted download fails closed');
equal(interruptedAdapter.applyCalls, 0, 'interrupted artifact is not applied');

values.clear(); served = rawPolicy({ sequence: 4 });
const cachedAdapter = new FakeAdapter(); const cached = new AppUpdateService(cachedAdapter);
await cached.initialize(); await cached.checkForUpdates(); await cached.download();
const cacheRestart = new AppUpdateService(cachedAdapter); await cacheRestart.initialize();
equal(cacheRestart.getState().status, UpdateState.READY_TO_INSTALL, 'verified cached artifact recovers after process death');

values.clear(); served = rawPolicy({ sequence: 5 });
const cancelledAdapter = new FakeAdapter(); const cancelled = new AppUpdateService(cancelledAdapter);
await cancelled.initialize(); await cancelled.checkForUpdates(); await cancelled.download(); await cancelled.apply();
equal(cancelled.getState().status, UpdateState.RESTART_REQUIRED, 'committed install waits for Android result');
await assert.rejects(cancelled.apply(), (error) => error.code === 'UPDATE_LOCKED'); assertions += 1;
equal(cancelledAdapter.applyCalls, 1, 'repeat apply cannot create a duplicate installer session');
cancelledAdapter.installerState = { state: 'FAILED', code: 'INSTALL_CANCELLED', message: 'cancelled', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
await cancelled.reconcileInstallerState();
equal(cancelled.getState().status, UpdateState.FAILED, 'Android cancellation is reconciled');
equal(cancelled.getState().error.code, 'INSTALL_CANCELLED', 'installer failure remains machine-readable');
ok(cancelled.getState().downloaded, 'verified package remains available for retry');
cancelledAdapter.installerState = { state: 'IDLE' };
await cancelled.apply();
equal(cancelledAdapter.applyCalls, 2, 'cancelled install can retry the verified package');

cancelledAdapter.installerState = { state: 'COMMITTED', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
await cancelled.reconcileInstallerState();
equal(cancelled.getState().status, UpdateState.RESTART_REQUIRED, 'committed session never creates a duplicate install');
cancelledAdapter.installerState = { state: 'USER_ACTION_REQUIRED', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
await cancelled.reconcileInstallerState();
equal(cancelled.getState().installerState, 'USER_ACTION_REQUIRED', 'Android confirmation state survives process lifecycle');

// A verified cache and startup-health marker survive JS process death.
const updatedAdapter = new FakeAdapter();
updatedAdapter.configuration = async () => ({ enabled: true, debug: false, policyUrlTemplate: 'https://updates.example/updates-{channel}.json', betaChannelAvailable: true,
  platform: 'android', architecture: 'arm64', sdk: 35, versionName: '1.0.1', buildNumber: 101 });
const restarted = new AppUpdateService(updatedAdapter);
await restarted.recordUpdatedLaunch();
let persisted = JSON.parse(values.get('seos.update.state.v1'));
equal(persisted.health.launchAttempts, 1, 'updated launch is recorded before normal initialization');
await restarted.initialize();
for (let attempt = 2; attempt <= 3; attempt += 1) {
  const crashed = new AppUpdateService(updatedAdapter);
  await crashed.recordUpdatedLaunch();
  if (attempt === 3) equal(crashed.getState().error.code, 'STARTUP_UNHEALTHY', 'three incomplete early launches are observable');
}

values.clear(); served = rawPolicy({ sequence: 6 });
const invalidAdapter = new FakeAdapter(); invalidAdapter.signatureValid = false;
const invalid = new AppUpdateService(invalidAdapter); await invalid.initialize();
await assert.rejects(invalid.checkForUpdates(), (error) => error.code === 'SIGNATURE_INVALID'); assertions += 1;
equal(invalid.getState().status, UpdateState.FAILED, 'invalid signature state');

values.clear(); served = rawPolicy({ sequence: 8 });
const rollbackAdapter = new FakeAdapter(); const rollback = new AppUpdateService(rollbackAdapter); await rollback.initialize(); await rollback.checkForUpdates({ manual: true });
served = rawPolicy({ sequence: 7 });
await assert.rejects(rollback.checkForUpdates({ manual: true }), (error) => error.code === 'METADATA_ROLLBACK'); assertions += 1;

values.clear(); served = rawPolicy({ sequence: 10 });
const equivocation = new AppUpdateService(new FakeAdapter()); await equivocation.initialize(); await equivocation.checkForUpdates({ manual: true });
await equivocation.checkForUpdates({ manual: true });
served = rawPolicy({ sequence: 10, hash: 'b'.repeat(64) });
await assert.rejects(equivocation.checkForUpdates({ manual: true }), (error) => error.code === 'METADATA_EQUIVOCATION'); assertions += 1;

values.clear(); served = rawPolicy({ sequence: 11 });
const unavailableBetaAdapter = new FakeAdapter();
unavailableBetaAdapter.configuration = async () => ({ enabled: true, debug: false, policyUrlTemplate: 'https://updates.example/updates-{channel}.json', betaChannelAvailable: false,
  platform: 'android', architecture: 'arm64', sdk: 35, versionName: '1.0.0', buildNumber: 100 });
const unavailableBeta = new AppUpdateService(unavailableBetaAdapter); await unavailableBeta.initialize();
await assert.rejects(unavailableBeta.setPreferences({ channel: 'BETA' }), (error) => error.code === 'INVALID_PREFERENCE'); assertions += 1;

values.clear(); served = rawPolicy({ sequence: 14, minVersion: '1.0.1' });
const oldSdkAdapter = new FakeAdapter();
oldSdkAdapter.configuration = async () => ({ enabled: true, debug: false, policyUrlTemplate: 'https://updates.example/updates-{channel}.json', betaChannelAvailable: false,
  platform: 'android', architecture: 'arm64', sdk: 23, versionName: '1.0.0', buildNumber: 100 });
const oldSdk = new AppUpdateService(oldSdkAdapter); await oldSdk.initialize(); await oldSdk.checkForUpdates({ manual: true });
equal(oldSdk.getState().target, null, 'incompatible OS has no install target');
equal(oldSdk.getState().compatibility, 'UNSUPPORTED', 'incompatible OS still enforces signed minimum client compatibility');

values.clear(); served = rawPolicy({ sequence: 12 });
const switched = new AppUpdateService(new FakeAdapter()); await switched.initialize(); await switched.checkForUpdates(); await switched.download();
served = rawPolicy({ sequence: 1, channel: 'BETA', version: '1.1.0-beta.1', build: 102 });
await switched.setPreferences({ channel: 'BETA' });
equal(switched.getState().target.release.channel, 'BETA', 'stable to beta performs a fresh check');
equal(switched.getState().downloaded, null, 'stable download is cleared on beta switch');
served = rawPolicy({ sequence: 13, channel: 'STABLE', version: '1.1.0', build: 103 });
await switched.setPreferences({ channel: 'STABLE' });
equal(switched.getState().target.release.channel, 'STABLE', 'beta to stable performs a fresh check');

values.clear(); served = rawPolicy({ sequence: 9 }); holdFetch = new Promise((resolve) => setTimeout(resolve, 25));
const locked = new AppUpdateService(new FakeAdapter()); await locked.initialize();
const first = locked.checkForUpdates({ manual: true });
await assert.rejects(locked.checkForUpdates({ manual: true }), (error) => error.code === 'UPDATE_LOCKED'); assertions += 1;
await first; holdFetch = null;

values.clear();
const nativeRecoveryAdapter = new FakeAdapter();
nativeRecoveryAdapter.installerState = { state: 'COMMITTED', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
const nativeRecovery = new AppUpdateService(nativeRecoveryAdapter); await nativeRecovery.initialize(); await nativeRecovery.reconcileInstallerState();
equal(nativeRecovery.getState().status, UpdateState.RESTART_REQUIRED, 'native durable session reconstructs a lost JS pending marker');
persisted = JSON.parse(values.get('seos.update.state.v1'));
equal(persisted.pending.buildNumber, 101, 'reconstructed installer identity is persisted');

// Pre-0.7.4 state: only per-source sequences were stored. The rollback floor survives the upgrade.
values.clear();
values.set('seos.update.state.v1', JSON.stringify({ installationId: 'legacy-install', channel: 'STABLE',
  sequences: { 'https://updates.example/updates-stable.json': 20 } }));
served = rawPolicy({ sequence: 19 });
const legacyFloor = new AppUpdateService(new FakeAdapter()); await legacyFloor.initialize();
await assert.rejects(legacyFloor.checkForUpdates({ manual: true }), (error) => error.code === 'METADATA_ROLLBACK'); assertions += 1;
served = rawPolicy({ sequence: 21 });
await legacyFloor.checkForUpdates({ manual: true });
persisted = JSON.parse(values.get('seos.update.state.v1'));
equal(persisted.acceptedPolicies['https://updates.example/updates-stable.json'].sequence, 21, 'legacy floor migrates to accepted-policy records');

// 0.7.3 → new build: health/pending markers without pendingBuild and a native record without a digest.
values.clear();
values.set('seos.update.state.v1', JSON.stringify({ installationId: 'legacy-install', channel: 'STABLE',
  pending: { version: '1.0.1', buildNumber: 101, sha256: 'a'.repeat(64) },
  health: { pendingVersion: '1.0.1', launchAttempts: 0, startupHealth: 'pending', rollbackCompatibility: 'BINARY_ONLY' } }));
const legacyUpgraded = new AppUpdateService(updatedAdapter);
updatedAdapter.installerState = { state: 'INSTALLED', targetVersion: '1.0.1', targetBuild: 101, targetSha256: '' };
await legacyUpgraded.recordUpdatedLaunch();
equal(JSON.parse(values.get('seos.update.state.v1')).health.launchAttempts, 1, 'legacy health marker is counted');
await legacyUpgraded.initialize(); await legacyUpgraded.reconcileInstallerState();
persisted = JSON.parse(values.get('seos.update.state.v1'));
equal(persisted.pending, null, 'legacy pending marker is cleared after the installed build runs');
equal(persisted.health.startupHealth, 'healthy', 'legacy upgrade reaches healthy state');

// An OS prompt that cannot be reopened becomes a retryable failure, never a dead end.
values.clear(); served = rawPolicy({ sequence: 30 });
const promptAdapter = new FakeAdapter();
promptAdapter.continueInstaller = async () => {
  promptAdapter.installerState = { state: 'FAILED', code: 'INSTALL_CANCELLED', message: 'cannot reopen', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
  throw new UpdateError('INSTALL_CANCELLED', 'cannot reopen');
};
const prompt = new AppUpdateService(promptAdapter); await prompt.initialize(); await prompt.checkForUpdates(); await prompt.download(); await prompt.apply();
promptAdapter.installerState = { state: 'USER_ACTION_REQUIRED', targetVersion: '1.0.1', targetBuild: 101, targetSha256: 'a'.repeat(64) };
await prompt.reconcileInstallerState();
await assert.rejects(prompt.continueInstaller(), (error) => error.code === 'INSTALL_CANCELLED'); assertions += 1;
equal(prompt.getState().status, UpdateState.FAILED, 'unreopenable installer prompt is reconciled to FAILED');
promptAdapter.installerState = { state: 'IDLE' };
await prompt.apply();
equal(promptAdapter.applyCalls, 2, 'verified package can be installed again after an abandoned prompt');

console.log(JSON.stringify({ ok: true, assertions }));
