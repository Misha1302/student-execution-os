import { t } from './i18n.js';
import { esc, icon, openSheet } from './ui.js';

export const TUTORIAL_KEY = 'seos.tutorial.capture-v1';

export function tutorialCompleted() {
  try { return localStorage.getItem(TUTORIAL_KEY) === 'done'; } catch { return false; }
}

export function completeTutorial() {
  try { localStorage.setItem(TUTORIAL_KEY, 'done'); } catch { /* storage may be unavailable */ }
}

export function openTutorial({ onDone = null, force = false } = {}) {
  if (!force && tutorialCompleted()) return null;
  const steps = [
    ['plus', t('tutorial.step1.title'), t('tutorial.step1.body')],
    ['mic', t('tutorial.step2.title'), t('tutorial.step2.body')],
    ['check', t('tutorial.step3.title'), t('tutorial.step3.body')],
    ['calendar', t('tutorial.step4.title'), t('tutorial.step4.body')],
  ];
  const dialog = openSheet({
    title: t('tutorial.title'),
    body: `<ol class="tutorial-steps">${steps.map(([name, title, body]) => `<li>
      <span class="menu-icon tone-accent">${icon(name)}</span><div><strong>${esc(title)}</strong><p>${esc(body)}</p></div>
    </li>`).join('')}</ol>`,
    actions: `<button type="button" class="button ghost" data-tutorial-skip>${esc(t('tutorial.skip'))}</button>
      <button type="button" class="button primary" data-tutorial-done>${esc(t('tutorial.done'))}</button>`,
  });
  let finished = false;
  const finish = (value) => {
    if (finished) return;
    finished = true;
    completeTutorial();
    dialog.close(value);
    onDone?.();
  };
  dialog.querySelector('[data-tutorial-skip]').addEventListener('click', () => finish('skipped'));
  dialog.querySelector('[data-tutorial-done]').addEventListener('click', () => finish('done'));
  dialog.addEventListener('close', () => { if (!finished) { completeTutorial(); finished = true; } });
  return dialog;
}
