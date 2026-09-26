// Product-owned update policy/domain. Hosting only supplies bytes; this module
// decides whether a signed exact target is eligible to run.

export const UpdateChannel = Object.freeze({ STABLE: 'STABLE', BETA: 'BETA' });
export const ReleaseStatus = Object.freeze({ DRAFT: 'DRAFT', AVAILABLE: 'AVAILABLE', PAUSED: 'PAUSED', WITHDRAWN: 'WITHDRAWN' });
export const MandatoryMode = Object.freeze({ OPTIONAL: 'OPTIONAL', REQUIRED_AFTER: 'REQUIRED_AFTER', UNSUPPORTED_CLIENT: 'UNSUPPORTED_CLIENT' });
export const UpdateState = Object.freeze({
  IDLE: 'IDLE', CHECKING: 'CHECKING', UP_TO_DATE: 'UP_TO_DATE', AVAILABLE: 'AVAILABLE',
  DOWNLOADING: 'DOWNLOADING', DOWNLOADED: 'DOWNLOADED', READY_TO_INSTALL: 'READY_TO_INSTALL',
  APPLYING: 'APPLYING', RESTART_REQUIRED: 'RESTART_REQUIRED', FAILED: 'FAILED',
});

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
const SHA256 = /^[0-9a-f]{64}$/;
const VALUES = (object) => new Set(Object.values(object));

export class UpdateError extends Error {
  constructor(code, message, { retryable = false, cause = null } = {}) {
    super(message || code, cause ? { cause } : undefined);
    this.name = 'UpdateError';
    this.code = code;
    this.retryable = retryable;
  }
}

export class SemVer {
  constructor(value) {
    const match = SEMVER.exec(String(value));
    if (!match) throw new UpdateError('METADATA_INVALID', `Invalid SemVer: ${value}`);
    this.major = Number(match[1]); this.minor = Number(match[2]); this.patch = Number(match[3]);
    this.prerelease = match[4] ? match[4].split('.') : [];
    this.build = match[5] ? match[5].split('.') : [];
    if (this.prerelease.some((part) => /^\d+$/.test(part) && part.length > 1 && part.startsWith('0'))) {
      throw new UpdateError('METADATA_INVALID', `Invalid numeric prerelease identifier: ${value}`);
    }
    this.value = String(value);
    Object.freeze(this.prerelease); Object.freeze(this.build); Object.freeze(this);
  }

  get isPrerelease() { return this.prerelease.length > 0; }

  compare(otherValue) {
    const other = otherValue instanceof SemVer ? otherValue : new SemVer(otherValue);
    for (const key of ['major', 'minor', 'patch']) {
      if (this[key] !== other[key]) return this[key] < other[key] ? -1 : 1;
    }
    if (!this.prerelease.length || !other.prerelease.length) {
      if (this.prerelease.length === other.prerelease.length) return 0;
      return this.prerelease.length ? -1 : 1;
    }
    const length = Math.max(this.prerelease.length, other.prerelease.length);
    for (let i = 0; i < length; i += 1) {
      if (this.prerelease[i] === undefined) return -1;
      if (other.prerelease[i] === undefined) return 1;
      const left = this.prerelease[i]; const right = other.prerelease[i];
      if (left === right) continue;
      const ln = /^\d+$/.test(left); const rn = /^\d+$/.test(right);
      if (ln && rn) return Number(left) < Number(right) ? -1 : 1;
      if (ln !== rn) return ln ? -1 : 1;
      return left < right ? -1 : 1;
    }
    return 0;
  }

  toString() { return this.value; }
}

function object(raw, name) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new UpdateError('METADATA_INVALID', `${name} must be an object`);
  return raw;
}

function exactKeys(raw, required, optional, name) {
  const keys = new Set(Object.keys(object(raw, name)));
  for (const key of required) if (!keys.has(key)) throw new UpdateError('METADATA_INVALID', `${name}.${key} is required`);
  for (const key of keys) if (!required.includes(key) && !optional.includes(key)) throw new UpdateError('METADATA_INVALID', `${name}.${key} is unknown`);
}

function enumValue(value, allowed, name) {
  if (!allowed.has(value)) throw new UpdateError('METADATA_INVALID', `Invalid ${name}`);
  return value;
}

function integer(value, min, max, name) {
  if (!Number.isInteger(value) || value < min || value > max) throw new UpdateError('METADATA_INVALID', `Invalid ${name}`);
  return value;
}

function instant(value, name) {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) throw new UpdateError('METADATA_INVALID', `Invalid ${name}`);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) throw new UpdateError('METADATA_INVALID', `Invalid ${name}`);
  return parsed;
}

function notes(raw) {
  exactKeys(raw, ['summary', 'changes'], [], 'release_notes locale');
  if (typeof raw.summary !== 'string' || !raw.summary.trim() || raw.summary.length > 500
      || !Array.isArray(raw.changes) || !raw.changes.length
      || raw.changes.some((item) => typeof item !== 'string' || !item.trim() || item.length > 500)) {
    throw new UpdateError('METADATA_INVALID', 'Invalid release notes');
  }
  return Object.freeze({ summary: raw.summary, changes: Object.freeze([...raw.changes]) });
}

export class UpdateArtifact {
  constructor(raw) {
    exactKeys(raw, ['platform', 'architecture', 'artifact_kind', 'url', 'size_bytes', 'sha256', 'updater_metadata'], [], 'artifact');
    if (raw.platform !== 'android' || !['universal', 'arm64', 'x64'].includes(raw.architecture) || raw.artifact_kind !== 'APK') {
      throw new UpdateError('INCOMPATIBLE_PLATFORM', 'Unsupported artifact identity');
    }
    let url;
    try { url = new URL(raw.url); } catch { throw new UpdateError('METADATA_INVALID', 'Invalid artifact URL'); }
    const localHttp = url.protocol === 'http:' && ['127.0.0.1', 'localhost', '10.0.2.2'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !localHttp) || url.username || url.password || url.hash) throw new UpdateError('METADATA_INVALID', 'Artifact URL must be public HTTPS or a local test URL');
    exactKeys(raw.updater_metadata, ['package_name', 'version_code', 'min_sdk'], [], 'updater_metadata');
    if (!/^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$/.test(raw.updater_metadata.package_name)) throw new UpdateError('METADATA_INVALID', 'Invalid package name');
    this.platform = raw.platform;
    this.architecture = raw.architecture;
    this.artifactKind = raw.artifact_kind;
    this.url = url.toString();
    this.sizeBytes = integer(raw.size_bytes, 1, 500 * 1024 * 1024, 'artifact size');
    if (typeof raw.sha256 !== 'string' || !SHA256.test(raw.sha256)) throw new UpdateError('METADATA_INVALID', 'Invalid SHA-256');
    this.sha256 = raw.sha256;
    this.updaterMetadata = Object.freeze({
      packageName: raw.updater_metadata.package_name,
      versionCode: integer(raw.updater_metadata.version_code, 1, Number.MAX_SAFE_INTEGER, 'version_code'),
      minSdk: integer(raw.updater_metadata.min_sdk, 24, 1000, 'min_sdk'),
    });
    this.raw = Object.freeze(structuredClone(raw));
    Object.freeze(this);
  }

  get identity() { return `${this.platform}-${this.architecture}-${this.artifactKind.toLowerCase()}`; }
}

export class UpdateRelease {
  constructor(raw) {
    exactKeys(raw, ['version', 'build_number', 'channel', 'published_at', 'status', 'severity', 'release_notes',
      'minimum_os_versions', 'mandatory_policy', 'rollback_compatibility', 'artifacts'], ['minimum_api_version'], 'release');
    this.version = new SemVer(raw.version);
    this.buildNumber = integer(raw.build_number, 1, Number.MAX_SAFE_INTEGER, 'build_number');
    this.channel = enumValue(raw.channel, VALUES(UpdateChannel), 'release channel');
    this.publishedAt = instant(raw.published_at, 'published_at');
    this.status = enumValue(raw.status, VALUES(ReleaseStatus), 'release status');
    this.severity = enumValue(raw.severity, new Set(['NORMAL', 'IMPORTANT', 'SECURITY', 'CRITICAL']), 'severity');
    object(raw.release_notes, 'release_notes');
    const locales = Object.keys(raw.release_notes);
    if (!locales.length || locales.some((locale) => !['en', 'ru'].includes(locale))) throw new UpdateError('METADATA_INVALID', 'Invalid release-note locales');
    this.releaseNotes = Object.freeze(Object.fromEntries(locales.map((locale) => [locale, notes(raw.release_notes[locale])])));
    exactKeys(raw.minimum_os_versions, [], ['android_sdk'], 'minimum_os_versions');
    this.minimumAndroidSdk = raw.minimum_os_versions.android_sdk == null ? null : integer(raw.minimum_os_versions.android_sdk, 24, 1000, 'android_sdk');
    this.minimumApiVersion = raw.minimum_api_version == null ? null : integer(raw.minimum_api_version, 1, Number.MAX_SAFE_INTEGER, 'minimum_api_version');
    exactKeys(raw.mandatory_policy, ['mode'], ['required_after'], 'mandatory_policy');
    const mode = enumValue(raw.mandatory_policy.mode, VALUES(MandatoryMode), 'mandatory mode');
    const requiredAfter = raw.mandatory_policy.required_after == null ? null : instant(raw.mandatory_policy.required_after, 'required_after');
    if ((mode === MandatoryMode.REQUIRED_AFTER) !== Boolean(requiredAfter)) throw new UpdateError('METADATA_INVALID', 'Invalid required_after policy');
    this.mandatoryPolicy = Object.freeze({ mode, requiredAfter });
    this.rollbackCompatibility = enumValue(raw.rollback_compatibility, new Set(['FULL', 'BINARY_ONLY', 'NOT_SUPPORTED']), 'rollback compatibility');
    if (!Array.isArray(raw.artifacts) || !raw.artifacts.length) throw new UpdateError('METADATA_INVALID', 'Release has no artifacts');
    this.artifacts = Object.freeze(raw.artifacts.map((artifact) => new UpdateArtifact(artifact)));
    if (new Set(this.artifacts.map((artifact) => artifact.identity)).size !== this.artifacts.length) throw new UpdateError('METADATA_INVALID', 'Duplicate artifact identity');
    if (this.artifacts.some((artifact) => artifact.updaterMetadata.versionCode !== this.buildNumber)) throw new UpdateError('METADATA_INVALID', 'Artifact build mismatch');
    if (this.channel === UpdateChannel.STABLE && this.version.isPrerelease) throw new UpdateError('METADATA_INVALID', 'Stable prerelease is forbidden');
    this.raw = Object.freeze(structuredClone(raw));
    Object.freeze(this);
  }

  get releaseId() { return `${this.channel}:${this.version}:${this.buildNumber}`; }
}

export class UpdatePolicy {
  constructor(raw) {
    exactKeys(raw, ['schema_version', 'sequence', 'generated_at', 'expires_at', 'channel', 'latest_version', 'rollout', 'releases', 'signature_metadata'], ['minimum_supported_version'], 'policy');
    this.schemaVersion = integer(raw.schema_version, 1, 1, 'schema_version');
    this.sequence = integer(raw.sequence, 1, Number.MAX_SAFE_INTEGER, 'sequence');
    this.generatedAt = instant(raw.generated_at, 'generated_at');
    this.expiresAt = instant(raw.expires_at, 'expires_at');
    if (this.expiresAt <= this.generatedAt) throw new UpdateError('METADATA_INVALID', 'Invalid policy validity interval');
    this.channel = enumValue(raw.channel, VALUES(UpdateChannel), 'policy channel');
    this.latestVersion = new SemVer(raw.latest_version);
    this.minimumSupportedVersion = raw.minimum_supported_version == null ? null : new SemVer(raw.minimum_supported_version);
    exactKeys(raw.rollout, ['percentage'], [], 'rollout');
    this.rollout = Object.freeze({ percentage: integer(raw.rollout.percentage, 0, 100, 'rollout percentage') });
    if (!Array.isArray(raw.releases) || !raw.releases.length) throw new UpdateError('METADATA_INVALID', 'Policy has no releases');
    this.releases = Object.freeze(raw.releases.map((release) => new UpdateRelease(release)));
    if (this.releases.some((release) => release.channel !== this.channel)) throw new UpdateError('METADATA_INVALID', 'Policy/release channel mismatch');
    if (this.channel === UpdateChannel.STABLE && this.latestVersion.isPrerelease) throw new UpdateError('METADATA_INVALID', 'Stable policy targets a prerelease');
    const matching = this.releases.filter((release) => release.version.toString() === this.latestVersion.toString());
    if (matching.length !== 1) throw new UpdateError('METADATA_INVALID', 'latest_version must match exactly one release');
    exactKeys(raw.signature_metadata, ['key_id', 'algorithm', 'signature'], [], 'signature_metadata');
    if (!/^[A-Za-z0-9._-]{1,64}$/.test(raw.signature_metadata.key_id) || raw.signature_metadata.algorithm !== 'Ed25519'
        || typeof raw.signature_metadata.signature !== 'string') throw new UpdateError('METADATA_INVALID', 'Invalid signature metadata');
    this.signatureMetadata = Object.freeze({ ...raw.signature_metadata });
    this.raw = Object.freeze(structuredClone(raw));
    Object.freeze(this);
  }

  get latestRelease() { return this.releases.find((release) => release.version.toString() === this.latestVersion.toString()); }
}

function canonicalValue(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalValue).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalValue(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function canonicalPolicy(raw) {
  const copy = structuredClone(raw);
  if (!copy?.signature_metadata || typeof copy.signature_metadata !== 'object') throw new UpdateError('METADATA_INVALID', 'Missing signature metadata');
  delete copy.signature_metadata.signature;
  return canonicalValue(copy);
}

// Synchronous SHA-256 is intentionally not implemented here. Web Crypto/native
// verifies signatures and artifacts; rollout uses this compact, specified FNV-1a
// bucket solely for deterministic cohort assignment (never as a trust primitive).
export function rolloutBucket(installationId, releaseId) {
  const bytes = new TextEncoder().encode(`${installationId}\n${releaseId}`);
  let hash = 0x811c9dc5;
  for (const byte of bytes) { hash ^= byte; hash = Math.imul(hash, 0x01000193) >>> 0; }
  return hash % 10000;
}

export function selectExactTarget(policy, { currentVersion, platform, architecture, sdk, installationId, now = new Date() }) {
  const current = currentVersion instanceof SemVer ? currentVersion : new SemVer(currentVersion);
  if (policy.expiresAt <= now) throw new UpdateError('METADATA_EXPIRED', 'Update policy has expired');
  const release = policy.latestRelease;
  if (release.status !== ReleaseStatus.AVAILABLE) return null;
  if (policy.channel === UpdateChannel.STABLE && release.version.isPrerelease) return null;
  if (release.version.compare(current) <= 0) return null; // forward fix by default; never automatic downgrade
  if (release.minimumAndroidSdk != null && sdk < release.minimumAndroidSdk) return null;
  const artifacts = release.artifacts.filter((item) => item.platform === platform && (item.architecture === architecture || item.architecture === 'universal'));
  const exact = artifacts.find((item) => item.architecture === architecture);
  const universal = artifacts.find((item) => item.architecture === 'universal');
  const artifact = exact || universal;
  if (!artifact || (exact && artifacts.filter((item) => item.architecture === architecture).length !== 1)
      || (!exact && artifacts.filter((item) => item.architecture === 'universal').length !== 1)) return null;
  let mandatory = release.mandatoryPolicy.mode;
  if (mandatory === MandatoryMode.REQUIRED_AFTER && release.mandatoryPolicy.requiredAfter > now) mandatory = MandatoryMode.OPTIONAL;
  if (policy.minimumSupportedVersion && current.compare(policy.minimumSupportedVersion) < 0) mandatory = MandatoryMode.UNSUPPORTED_CLIENT;
  // Staging is a safety tool for optional adoption. It must not strand a
  // client which policy explicitly declares unsupported or already required.
  if (mandatory === MandatoryMode.OPTIONAL
      && rolloutBucket(installationId, release.releaseId) >= policy.rollout.percentage * 100) return null;
  return Object.freeze({ policy, release, artifact, mandatory });
}

const TRANSITIONS = Object.freeze({
  IDLE: new Set(['CHECKING', 'READY_TO_INSTALL']), // verified cache recovery after process restart
  CHECKING: new Set(['UP_TO_DATE', 'AVAILABLE', 'READY_TO_INSTALL', 'FAILED']),
  UP_TO_DATE: new Set(['CHECKING']),
  AVAILABLE: new Set(['CHECKING', 'DOWNLOADING']),
  DOWNLOADING: new Set(['AVAILABLE', 'DOWNLOADED', 'FAILED']),
  DOWNLOADED: new Set(['READY_TO_INSTALL', 'FAILED']),
  READY_TO_INSTALL: new Set(['CHECKING', 'APPLYING', 'FAILED']),
  APPLYING: new Set(['RESTART_REQUIRED', 'IDLE', 'FAILED']),
  RESTART_REQUIRED: new Set(['CHECKING', 'APPLYING', 'IDLE', 'FAILED']),
  FAILED: new Set(['CHECKING', 'AVAILABLE', 'DOWNLOADING', 'READY_TO_INSTALL', 'APPLYING', 'IDLE']),
});

export function transition(state, next) {
  if (!VALUES(UpdateState).has(state) || !VALUES(UpdateState).has(next) || !TRANSITIONS[state]?.has(next)) {
    throw new UpdateError('INVALID_STATE', `Invalid update transition ${state} -> ${next}`);
  }
  return next;
}
