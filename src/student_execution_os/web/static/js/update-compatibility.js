// Small dependency-free bridge between the signed update-policy owner and API
// transport. Offline/local data and the updater remain available when a signed
// policy declares the running client unsupported.

let state = 'SUPPORTED';

export function setClientCompatibility(next) {
  if (!['SUPPORTED', 'UPDATE_REQUIRED_SOON', 'UNSUPPORTED'].includes(next)) {
    throw new TypeError(`Invalid client compatibility state: ${next}`);
  }
  state = next;
  globalThis.window?.dispatchEvent?.(new CustomEvent('seos-client-compatibility', { detail: { state } }));
}

export function getClientCompatibility() { return state; }

export function onlineOperationAllowed(path) {
  if (state !== 'UNSUPPORTED') return true;
  // These endpoints provide recovery/bootstrap only. Account export remains
  // available so an unsupported client never traps user-owned data.
  return path === '/api/v1/health'
    || path.startsWith('/api/v1/auth/')
    || path === '/api/v1/account/export';
}
