import { t, fmtDuration } from './i18n.js';
import { invalidate } from './store.js';
import { toast, errorMessage, confirmSheet, openSheet, chipGroup, chipValue, esc, setBusy } from './ui.js';
import { haptic } from './native.js';
import { queueOperation } from './sync.js';

export const UNDO_MS = 10000;

// Hooks the shell installs so mutations can re-render without importing the router.
export const shell = { rerender: async () => {}, go: () => {} };

// Online-only requests (attachments, series, account settings): wait for the server.
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

// A change to a task or event. It is queued durably and shown at once (the overlay
// projects it onto every cached screen); the network is never awaited here, so the
// same tap works online, offline and on a flaky connection. The server's answer
// arrives later through sync.js and refreshes the screen in the background.
export async function change(type, entityId, payload = {}, { success, undo } = {}) {
  let result;
  try {
    result = queueOperation(type, entityId, payload);
  } catch (err) {
    toast(errorMessage(err), { error: true });
    return null;
  }
  haptic('LIGHT');
  // Undo stays offered for 10 seconds; it queues the inverse operation (same queue).
  if (success && !result.duplicate) toast(success, undo ? { action: { label: t('common.undo'), run: undo }, duration: UNDO_MS } : {});
  await shell.rerender(false);
  return result;
}

// For the user a task is in one of three places: in work, «Выполнено» or «Архив»
// («не буду делать» and archived both live in the archive). This is the label.
export function taskPlace(task) {
  if (!task) return 'open';
  if (task.status === 'ACTIVE' || task.status === 'DRAFT') return 'open';
  if (task.status === 'COMPLETED') return 'done';
  return 'archive';
}

export function lifecycle(id, _version, action, { title, kind = 'task', from = null } = {}) {
  // Undo puts the item back where it was: an archived open task returns to work.
  const undoOf = { complete: 'reopen', cancel: 'reopen', archive: from && from !== 'COMPLETED' ? 'restore' : 'unarchive',
    restore: from === 'ARCHIVED' ? 'archive' : from === 'CANCELLED' ? 'cancel' : null }[action];
  const run = () => change(`${kind}.${action}`, id, {}, {
    success: t(`lifecycle.done.${action}`),
    undo: undoOf ? () => change(`${kind}.${undoOf}`, id, {}) : null,
  });
  if (action === 'delete') {
    return confirmSheet({
      title: t(kind === 'event' ? 'lifecycle.deleteEventTitle' : 'lifecycle.deleteTitle'),
      body: `<p>${esc(t('lifecycle.deleteBody', { title: title || '' }))}</p>`,
      confirmLabel: t('lifecycle.deleteConfirm'),
      cancelLabel: t('common.keep'),
      danger: true,
    }).then((ok) => (ok ? change(`${kind}.delete`, id, {}, { success: t('lifecycle.done.delete') }).then((r) => {
      if (r && kind === 'task') shell.go('tasks');
      return r;
    }) : null));
  }
  if (action !== 'cancel') return run();
  return confirmSheet({
    title: t(kind === 'event' ? 'lifecycle.cancelEventTitle' : 'lifecycle.cancelTitle'),
    body: `<p>${esc(t(kind === 'event' ? 'lifecycle.cancelEventBody' : 'lifecycle.cancelBody', { title: title || '' }))}</p>`,
    confirmLabel: t(kind === 'event' ? 'lifecycle.cancelEventConfirm' : 'lifecycle.cancelConfirm'),
    cancelLabel: t('common.keep'),
    danger: true,
  }).then((ok) => (ok ? run() : null));
}

// "I worked on it": time spent and/or items done ("3 of 10 problems"); offers
// completion when nothing is left.
export function logProgress(task, suggested) {
  const remaining = Number(task.remaining_effort_minutes || 0);
  const count = task.count_progress;
  const options = [...new Set([15, 30, 45, 60, 90, Number(suggested) || 0].filter((m) => m > 0 && m <= Math.max(remaining, 15)))]
    .sort((a, b) => a - b)
    .map((m) => [m, fmtDuration(m)]);
  if (remaining > 0 && !options.some(([m]) => m === remaining)) options.push([remaining, t('progress.all')]);
  const preset = count ? '' : String(options.find(([m]) => m === Number(suggested))?.[0] ?? options[0]?.[0] ?? '');
  const countLeft = count ? count.total - count.done : 0;
  const countOptions = count ? [...new Set([1, 2, 3, 5, 10].filter((n) => n < countLeft)), countLeft].filter((n) => n > 0).map((n) => [String(n), n === countLeft ? t('progress.countAll', { n }) : `+${n}`]) : [];
  const dialog = openSheet({
    eyebrow: task.title,
    title: t('progress.title'),
    body: `${count ? `<p class="muted">${esc(t('progress.countNow', { done: count.done, total: count.total, unit: count.unit || '' }))}</p>
        <div class="field"><span>${esc(t('progress.countDone'))}</span>${chipGroup('count', countOptions, countOptions[0]?.[0] || '')}</div>` : ''}
      <p class="muted">${esc(t('progress.remaining', { d: fmtDuration(remaining) }))}</p>
      <div class="field"><span>${esc(t('progress.spent'))}</span>${chipGroup('spent', count ? [['', t('progress.noTime')], ...options] : options, preset)}</div>
      <p class="help">${esc(t('progress.help'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('progress.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const spent = Number(chipValue(dialog, 'spent') || 0);
    const items = count ? Number(chipValue(dialog, 'count') || 0) : 0;
    if (!spent && !items) { dialog.close('unchanged'); return; }
    const payload = {};
    if (spent) payload.minutes = spent;
    if (items) payload.count = items;
    let left = Math.max(0, remaining - spent);
    if (count && items && !spent) left = countLeft - items <= 0 ? 0 : Math.ceil(remaining * ((countLeft - items) / countLeft));
    const finished = count ? count.done + items >= count.total : left === 0;
    const updated = await change('task.progress', task.id, payload, {
      success: finished ? null : count ? t('progress.countSaved', { done: count.done + items, total: count.total }) : t('progress.saved', { d: fmtDuration(left) }),
    });
    dialog.close('saved');
    if (updated && finished) {
      const done = await confirmSheet({
        title: t('progress.finishedTitle'),
        body: `<p>${esc(t('progress.finishedBody'))}</p>`,
        confirmLabel: t('lifecycle.complete'),
        cancelLabel: t('progress.notYet'),
      });
      if (done) await lifecycle(task.id, null, 'complete');
    }
  });
}
