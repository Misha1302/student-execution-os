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

const browserSpeech = () => (isNative() ? null : window.SpeechRecognition || window.webkitSpeechRecognition || null);

export const voiceSupported = () => Boolean(plugin('SpeechRecognition') || browserSpeech());

// Browsers with the Web Speech API (Chrome, Edge, Safari) dictate without the app.
function browserSpeechToText(Recognition) {
  return new Promise((resolve, reject) => {
    const recognition = new Recognition();
    recognition.lang = SPEECH_LOCALES[String(document.documentElement.lang || 'ru').slice(0, 2)] || 'ru-RU';
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    let text = '';
    recognition.onresult = (event) => { text = String(event.results?.[0]?.[0]?.transcript || ''); };
    recognition.onerror = (event) => {
      if (event.error === 'not-allowed' || event.error === 'service-not-allowed') reject(new NativeError('VOICE_DENIED'));
      else if (event.error === 'no-speech' || event.error === 'aborted') resolve('');
      else reject(new NativeError('VOICE_FAILED', event.error));
    };
    recognition.onend = () => resolve(text.trim());
    recognition.start();
  });
}

// A dictation the user controls: it runs until they tap "Стоп" (or the recognizer
// stops by itself after silence) and reports what was heard so far. Returns
// { result: Promise<string>, stop() }. The text only fills the capture field —
// nothing is created from speech without the user's review.
export function startDictation({ onPartial = () => {}, onState = () => {} } = {}) {
  const lang = SPEECH_LOCALES[String(document.documentElement.lang || 'ru').slice(0, 2)] || 'ru-RU';
  const speech = plugin('SpeechRecognition');
  if (!speech) {
    const Recognition = browserSpeech();
    if (!Recognition) return { result: Promise.reject(new NativeError('VOICE_UNAVAILABLE')), stop() {} };
    const recognition = new Recognition();
    recognition.lang = lang;
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.maxAlternatives = 1;
    let finals = '';
    let interim = '';
    const result = new Promise((resolve, reject) => {
      recognition.onstart = () => onState('recording');
      recognition.onresult = (event) => {
        interim = '';
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const text = String(event.results[i][0]?.transcript || '');
          if (event.results[i].isFinal) finals = `${finals} ${text}`.trim(); else interim = `${interim} ${text}`.trim();
        }
        onPartial(`${finals} ${interim}`.trim());
      };
      recognition.onerror = (event) => {
        if (event.error === 'not-allowed' || event.error === 'service-not-allowed') reject(new NativeError('VOICE_DENIED'));
        else if (event.error !== 'no-speech' && event.error !== 'aborted') reject(new NativeError('VOICE_FAILED', event.error));
      };
      recognition.onend = () => resolve(`${finals} ${interim}`.trim());
    });
    try { recognition.start(); } catch (err) { return { result: Promise.reject(new NativeError('VOICE_FAILED', String(err?.message || ''))), stop() {} }; }
    return { result, stop: () => { onState('processing'); try { recognition.stop(); } catch { /* already stopped */ } } };
  }
  let latest = '';
  let finish;
  const handles = [];
  const result = (async () => {
    const available = await speech.available().catch(() => ({ available: false }));
    if (!available?.available) throw new NativeError('VOICE_UNAVAILABLE');
    let permission = await speech.checkPermissions().catch(() => null);
    if (permission?.speechRecognition !== 'granted') permission = await speech.requestPermissions().catch(() => null);
    if (permission?.speechRecognition !== 'granted') throw new NativeError('VOICE_DENIED');
    const done = new Promise((resolve) => { finish = resolve; });
    handles.push(await speech.addListener?.('partialResults', (data) => {
      latest = String(data?.matches?.[0] || latest);
      onPartial(latest);
    }));
    handles.push(await speech.addListener?.('listeningState', (data) => {
      if (data?.status === 'stopped') finish?.();
    }));
    onState('recording');
    try {
      const first = await speech.start({ language: lang, maxResults: 1, partialResults: true, popup: false });
      if (first?.matches?.[0]) { latest = String(first.matches[0]); finish?.(); }
    } catch (err) {
      if (!/no match|cancel|didn.t understand/i.test(String(err?.message || ''))) throw new NativeError('VOICE_FAILED', String(err?.message || ''));
      finish?.();
    }
    await done;
    return latest.trim();
  })().finally(() => handles.forEach((h) => h?.remove?.()));
  return {
    result,
    stop: () => { onState('processing'); speech.stop?.().catch?.(() => {}); setTimeout(() => finish?.(), 1500); },
  };
}

// Speech is text input only: the transcript goes through the same capture parse and
// card as typed text, and nothing is created until the user presses Create.
export async function speechToText() {
  const speech = plugin('SpeechRecognition');
  if (!speech) {
    const Recognition = browserSpeech();
    if (Recognition) return browserSpeechToText(Recognition);
    throw new NativeError('VOICE_UNAVAILABLE');
  }
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

// Taps on notifications the app rendered itself (and their "Reschedule" button)
// arrive as seos://open/<route> links: at cold start via getLaunchUrl, later as
// appUrlOpen events. The route is handed to the router unchanged.
export async function onAppLink(handler) {
  const app = plugin('App');
  if (!app) return;
  const route = (url) => {
    const match = /^seos:\/\/open\/(.+)$/.exec(String(url || ''));
    if (match) handler(match[1]);
  };
  await app.addListener?.('appUrlOpen', ({ url }) => route(url));
  const launch = await app.getLaunchUrl?.().catch(() => null);
  if (launch?.url) route(launch.url);
}

export function pushEnabled() {
  return isNative() && Boolean(window.SEOS_CONFIG?.pushEnabled) && Boolean(plugin('PushNotifications'));
}

let pushListeners = null;
let pushDenied = false;
export const pushWasDenied = () => pushDenied;

// Safe to call repeatedly (boot, after sign-in, after resume): listeners are
// installed once and register() re-emits the current token for the signed-in account.
export async function setupPush(onToken, onDeepLink) {
  const push = plugin('PushNotifications');
  if (!push || !pushEnabled()) return { configured: false };
  const permission = await push.requestPermissions();
  if (permission.receive !== 'granted') { pushDenied = true; return { configured: false, denied: true }; }
  pushDenied = false;
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

// ---- device health and alarms (the app's own SeosNative plugin, Android only) -----

const seos = () => plugin('SeosNative');

export const alarmsSupported = () => Boolean(seos());

// What this device can show right now. Browsers report the Notification permission;
// they never receive reminder pushes (only the Android app does).
export async function deviceStatus() {
  const native = seos();
  if (native) {
    try { return { native: true, ...(await native.status()) }; } catch { /* older build: fall through */ }
  }
  const push = plugin('PushNotifications');
  if (push) {
    const permission = await push.checkPermissions().catch(() => null);
    return { native: true, notifications: permission?.receive === 'granted', permission: permission?.receive || 'unknown' };
  }
  if (typeof Notification !== 'undefined') return { native: false, permission: Notification.permission };
  return { native: false, permission: 'unsupported' };
}

// Opens the system screen where the user can fix a permission:
// 'notifications' | 'exact_alarms' | 'full_screen' | 'battery'.
export async function openDeviceSettings(target) {
  const native = seos();
  if (!native) return false;
  await native.openSettings({ target });
  return true;
}

// Hands the device every upcoming alarm of the account. The native side replaces
// its schedule with this list (AlarmManager alarm clocks, kept across reboots).
// An unchanged list is not handed over again; the memo belongs to the device's
// schedule, so clearing that schedule forgets it.
let lastAlarmList = '';

export async function syncAlarms(alarms, labels = {}) {
  const native = seos();
  if (!native) return { supported: false };
  const signature = JSON.stringify({ alarms, labels });
  if (signature === lastAlarmList) return { unchanged: true };
  const result = await native.syncAlarms({ alarms, labels });
  lastAlarmList = signature;
  return result;
}

// Ends the signed-in account's ownership of this phone's alarms (logout, another
// account or server): scheduled and ringing ones stop; the local test alarm stays.
export async function clearDeviceAlarms() {
  lastAlarmList = '';
  const native = seos();
  if (!native?.clearAlarms) return { supported: false, removed: 0 };
  return native.clearAlarms();
}

export async function testAlarm() {
  const native = seos();
  if (!native) return false;
  await native.testAlarm();
  return true;
}
