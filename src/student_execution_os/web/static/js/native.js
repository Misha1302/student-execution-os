// Thin bridge to Capacitor plugins. Every call is a no-op in an ordinary browser,
// so the same bundle runs from the Python host and inside the Android shell.

const cap = () => window.Capacitor;

export const isNative = () => Boolean(cap()?.isNativePlatform?.());

export const plugin = (name) => (isNative() ? cap()?.Plugins?.[name] : undefined);

export async function prefGet(key) {
  const prefs = plugin('Preferences');
  if (prefs) {
    const { value } = await prefs.get({ key });
    return value ?? null;
  }
  try { return localStorage.getItem(key); } catch { return null; }
}

export async function prefSet(key, value) {
  const prefs = plugin('Preferences');
  if (prefs) {
    if (value == null) await prefs.remove({ key });
    else await prefs.set({ key, value: String(value) });
    return;
  }
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, String(value));
  } catch { /* storage unavailable: session stays in memory only */ }
}

export function haptic(style = 'LIGHT') {
  plugin('Haptics')?.impact?.({ style }).catch?.(() => {});
}

export function onBackButton(handler) {
  plugin('App')?.addListener?.('backButton', handler);
}

export function exitApp() {
  plugin('App')?.exitApp?.();
}

export function onResume(handler) {
  plugin('App')?.addListener?.('resume', handler);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') handler();
  });
}

export async function applyStatusBar(dark) {
  const bar = plugin('StatusBar');
  if (!bar) return;
  try {
    await bar.setStyle({ style: dark ? 'DARK' : 'LIGHT' });
    await bar.setBackgroundColor?.({ color: dark ? '#0f1216' : '#f6f7f9' });
  } catch { /* older WebView: ignore */ }
}

export function hideSplash() {
  plugin('SplashScreen')?.hide?.().catch?.(() => {});
}

const SPEECH_LOCALES = { ru: 'ru-RU', en: 'en-US' };

export class NativeError extends Error {
  constructor(code, message) { super(message || code); this.code = code; }
}

// Speech is text input only: the caller puts the transcript into an editable field
// and nothing is interpreted or applied until the user submits it.
export async function speechToText() {
  const speech = plugin('SpeechRecognition');
  if (!speech) throw new NativeError('VOICE_UNAVAILABLE');
  const available = await speech.available().catch(() => ({ available: false }));
  if (!available?.available) throw new NativeError('VOICE_UNAVAILABLE');
  let permission = await speech.checkPermissions().catch(() => null);
  if (permission?.speechRecognition !== 'granted') permission = await speech.requestPermissions().catch(() => null);
  if (permission?.speechRecognition !== 'granted') throw new NativeError('VOICE_DENIED');
  const lang = String(document.documentElement.lang || 'ru').slice(0, 2);
  let result;
  try {
    result = await speech.start({ language: SPEECH_LOCALES[lang] || 'ru-RU', maxResults: 1, partialResults: false, popup: false });
  } catch (err) {
    // The recognizer reports "no match"/cancel as errors; both mean "no text".
    if (/no match|cancel|didn.t understand/i.test(String(err?.message || ''))) return '';
    throw new NativeError('VOICE_FAILED', String(err?.message || ''));
  }
  return String(result?.matches?.[0] || '').trim();
}

export function pushEnabled() {
  return isNative() && Boolean(window.SEOS_CONFIG?.pushEnabled) && Boolean(plugin('PushNotifications'));
}

let pushListeners = null;

// Safe to call repeatedly (boot, after sign-in, after resume): listeners are
// installed once and register() re-emits the current token for the signed-in account.
export async function setupPush(onToken, onDeepLink) {
  const push = plugin('PushNotifications');
  if (!push || !pushEnabled()) return { configured: false };
  const permission = await push.requestPermissions();
  if (permission.receive !== 'granted') return { configured: false, denied: true };
  if (!pushListeners) pushListeners = installPushListeners(push, onToken, onDeepLink);
  await pushListeners;
  await push.register();
  return { configured: true };
}

async function installPushListeners(push, onToken, onDeepLink) {
  await push.addListener('registration', ({ value }) => onToken(value));
  await push.addListener('registrationError', (error) => console.warn('push registration failed', error));
  await push.addListener('pushNotificationReceived', (notification) => {
    // Android does not display a system notification while the app is in the
    // foreground. Surface it through the running UI and refresh the inbox.
    window.dispatchEvent(new CustomEvent('seos-push-received', { detail: notification }));
  });
  await push.addListener('pushNotificationActionPerformed', ({ notification }) => {
    const data = notification?.data || {};
    onDeepLink?.(data.deep_link || (data.task_id ? `#/task/${encodeURIComponent(data.task_id)}` : '#/today'));
  });
}

// Saves a JSON document: share sheet on Android, a regular download in browsers.
export async function saveJson(filename, text) {
  const fs = plugin('Filesystem');
  const share = plugin('Share');
  if (fs && share) {
    const { uri } = await fs.writeFile({ path: filename, data: text, directory: 'CACHE', encoding: 'utf8' });
    await share.share({ title: filename, files: [uri] });
    return;
  }
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
