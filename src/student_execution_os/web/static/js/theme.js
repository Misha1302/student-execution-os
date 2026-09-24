import { applyStatusBar } from './native.js';

const KEY = 'seos.theme';
const media = window.matchMedia?.('(prefers-color-scheme: dark)');

export function getTheme() {
  try { return localStorage.getItem(KEY) || 'system'; } catch { return 'system'; }
}

export function applyTheme(choice = getTheme()) {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
  const dark = choice === 'dark' || (choice === 'system' && media?.matches);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#0f1216' : '#f6f7f9');
  applyStatusBar(dark);
}

export function setTheme(choice) {
  try { localStorage.setItem(KEY, choice); } catch { /* ignore */ }
  applyTheme(choice);
}

media?.addEventListener?.('change', () => applyTheme());
