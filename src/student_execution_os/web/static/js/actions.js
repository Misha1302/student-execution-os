import { t, fmtDuration } from './i18n.js';
import { invalidate } from './store.js';
import { toast, errorMessage, confirmSheet, openSheet, chipGroup, chipValue, esc, setBusy } from './ui.js';
import { haptic } from './native.js';
import { queueOperation } from './sync.js';

// Hooks the shell installs so mutations can re-render without importing the router.
export const shell = { rerender: async () => {}, go: () => {} };

export async function mutate(run, { success, after } = {}) {
  try {
    const result = await run();
    invalidate();
    haptic('LIGHT');
    if (success) toast(success);
    await (after ? after(result) : shell.rerender(true));
    return result;
  } catch (err) {
    toast(errorMessage(err), { error: true });
    if (err.code === 'VERSION_CONFLICT') {
      invalidate();
      await shell.rerender(true);
    }
    return null;
  }
}

export function lifecycle(id, version, action, { title } = {}) {
  const run = () => mutate(
    async () => {
      const result = await queueOperation(`task.${action}`, id, {}, {
        optimisticTask: { id, version: Number(version) + 1, status: action === 'complete' ? 'COMPLETED' : action === 'cancel' ? 'CANCELLED' : 'ACTIVE' },
      });
      return result.entity || result;
    },
    { success: t(`lifecycle.done.${action}`) },
  );
  if (action !== 'cancel') return run();
  return confirmSheet({
    title: t('lifecycle.cancelTitle'),
    body: `<p>${esc(t('lifecycle.cancelBody', { title: title || '' }))}</p>`,
    confirmLabel: t('lifecycle.cancelConfirm'),
    cancelLabel: t('common.keep'),
    danger: true,
  }).then((ok) => (ok ? run() : null));
}

// "I worked on it": reduce remaining effort; offer completion when it reaches zero.
export function logProgress(task, suggested) {
  const remaining = Number(task.remaining_effort_minutes || 0);
  const options = [...new Set([15, 30, 45, 60, 90, Number(suggested) || 0].filter((m) => m > 0 && m <= Math.max(remaining, 15)))]
    .sort((a, b) => a - b)
    .map((m) => [m, fmtDuration(m)]);
  if (remaining > 0 && !options.some(([m]) => m === remaining)) options.push([remaining, t('progress.all')]);
  const preset = options.find(([m]) => m === Number(suggested))?.[0] ?? options[0]?.[0];
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('progress.title'),
    body: `<p class="muted">${esc(t('progress.remaining', { d: fmtDuration(remaining) }))}</p>
      ${chipGroup('spent', options, preset)}
      <p class="help">${esc(t('progress.help'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('progress.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const spent = Number(chipValue(dialog, 'spent') || 0);
    const left = Math.max(0, remaining - spent);
    setBusy(e.currentTarget, true);
    const updated = await mutate(
      async () => {
        const result = await queueOperation('task.progress', task.id, { minutes: spent }, {
          optimisticTask: { ...task, remaining_effort_minutes: left, started_at: task.started_at || new Date().toISOString(), last_progress_at: new Date().toISOString() },
        });
        return result.entity || result;
      },
      { success: left ? t('progress.saved', { d: fmtDuration(left) }) : null },
    );
    dialog.close('saved');
    if (updated && left === 0) {
      const done = await confirmSheet({
        title: t('progress.finishedTitle'),
        body: `<p>${esc(t('progress.finishedBody'))}</p>`,
        confirmLabel: t('lifecycle.complete'),
        cancelLabel: t('progress.notYet'),
      });
      if (done) await lifecycle(task.id, updated.version, 'complete');
    }
  });
}
