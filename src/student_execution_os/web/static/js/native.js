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

export async function speechToText() {
  const speech = plugin('SpeechRecognition');
  if (!speech) throw new Error('Speech recognition is unavailable');
  const permission = await speech.requestPermissions();
  if (!['granted', 'GRANTED'].includes(permission?.speechRecognition)) throw new Error('Microphone permission denied');
  const result = await speech.start({ language: document.documentElement.lang || 'ru-RU', maxResults: 1, partialResults: false });
  return result?.matches?.[0] || '';
}

export async function setupPush(onToken, onDeepLink) {
  const push = plugin('PushNotifications');
  if (!push) return { configured: false };
  const permission = await push.requestPermissions();
  if (permission.receive !== 'granted') return { configured: false, denied: true };
  await push.addListener('registration', ({ value }) => onToken(value));
  await push.addListener('registrationError', () => {});
  await push.addListener('pushNotificationActionPerformed', ({ notification }) => {
    const data = notification?.data || {};
    onDeepLink?.(data.deep_link || (data.task_id ? `#/task/${encodeURIComponent(data.task_id)}` : '#/today'));
  });
  await push.register();
  return { configured: true };
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
