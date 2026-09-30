import { openCapture } from './capture.js';
import { session } from './api.js';

export const TUTORIAL_KEY = 'seos.tutorial.capture-v1';

export const tutorialStorageKey = () => `${TUTORIAL_KEY}:${session.server || window.location?.origin || ''}:${session.user?.account_id || 'bound'}`;

export function tutorialCompleted() {
  try { return localStorage.getItem(tutorialStorageKey()) === 'done'; } catch { return false; }
}

export function completeTutorial() {
  try { localStorage.setItem(tutorialStorageKey(), 'done'); } catch { /* storage may be unavailable */ }
}

export function openTutorial({ onDone = null, force = false } = {}) {
  if (!force && tutorialCompleted()) return null;
  const dialog = openCapture({ guided: true });
  dialog.addEventListener('close', () => { if (dialog.returnValue === 'saved') onDone?.(); });
  return dialog;
}
