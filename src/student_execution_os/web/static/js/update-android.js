import { isNative, plugin } from './native.js';
import { UpdateError, canonicalPolicy } from './update-domain.js';

function nativeError(error, fallback = 'UPDATE_FAILED') {
  const code = String(error?.code || fallback);
  return new UpdateError(code, String(error?.message || code), {
    retryable: ['NETWORK_ERROR', 'METADATA_UNAVAILABLE', 'DOWNLOAD_INTERRUPTED'].includes(code), cause: error,
  });
}

export class AndroidUpdateAdapter {
  constructor() { this.bridge = plugin('SeosUpdate'); }

  get available() { return isNative() && Boolean(this.bridge); }

  async configuration() {
    if (!this.available) return { enabled: false, platform: 'web' };
    try { return await this.bridge.configuration(); } catch (error) { throw nativeError(error, 'UPDATER_UNAVAILABLE'); }
  }

  async verifyPolicy(rawPolicy) {
    try {
      return await this.bridge.verifyPolicy({
        canonicalPolicy: canonicalPolicy(rawPolicy),
        keyId: rawPolicy.signature_metadata?.key_id,
        algorithm: rawPolicy.signature_metadata?.algorithm,
        signature: rawPolicy.signature_metadata?.signature,
      });
    } catch (error) { throw nativeError(error, 'SIGNATURE_INVALID'); }
  }

  async onProgress(handler) {
    if (!this.bridge?.addListener) return { remove() {} };
    return this.bridge.addListener('downloadProgress', handler);
  }

  async download(target) {
    try {
      return await this.bridge.download({
        version: target.release.version.toString(), buildNumber: target.release.buildNumber,
        url: target.artifact.url, sizeBytes: target.artifact.sizeBytes, sha256: target.artifact.sha256,
        packageName: target.artifact.updaterMetadata.packageName,
      });
    } catch (error) { throw nativeError(error, 'DOWNLOAD_FAILED'); }
  }

  async verifyDownloaded(target, downloaded) {
    try {
      return await this.bridge.verifyDownloaded({
        path: downloaded.path, version: target.release.version.toString(), buildNumber: target.release.buildNumber,
        sizeBytes: target.artifact.sizeBytes, sha256: target.artifact.sha256,
        packageName: target.artifact.updaterMetadata.packageName,
      });
    } catch (error) { throw nativeError(error, 'HASH_MISMATCH'); }
  }

  async apply(target, downloaded) {
    try {
      return await this.bridge.apply({
        path: downloaded.path, version: target.release.version.toString(), buildNumber: target.release.buildNumber,
        sizeBytes: target.artifact.sizeBytes, sha256: target.artifact.sha256,
        packageName: target.artifact.updaterMetadata.packageName,
      });
    } catch (error) { throw nativeError(error, 'INSTALLER_FAILED'); }
  }

  async openInstallPermission() {
    try { return await this.bridge.openInstallPermission(); } catch (error) { throw nativeError(error, 'PERMISSION_REQUIRED'); }
  }

  async nativeState() {
    try { return await this.bridge.getState(); } catch (error) { throw nativeError(error); }
  }
}
