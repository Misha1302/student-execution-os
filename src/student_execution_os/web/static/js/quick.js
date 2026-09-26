// Quick actions on anything in a list: long press on a phone, right click on a
// desktop. The actions depend on what the item is and where it is in its life
// (a task in work, done or archived; an event; a reminder). A finished task can be
// swiped away into the archive, with Undo for 10 seconds.
//
// Every action is one of the existing queued operations (actions.js / sync.js); this
// module only chooses which one and asks for confirmation where the product does.
import { peek } from './store.js';
import { t } from './i18n.js';
import { actionSheet } from './ui.js';
import { change, lifecycle, logProgress, taskPlace } from './actions.js';
import { haptic } from './native.js';
import { rescheduleSheet, editTaskSheet } from './capture.js';
import { eventSheet } from './events.js';
import { reminderSheet, reminderAction, snoozeChoices, remindAboutSheet, isOpen } from './reminders.js';

// The entity an element stands for: explicit data-kind, or the existing open actions.
export function entityRef(el) {
  const node = el?.closest?.('[data-kind][data-id], [data-action="open-task"][data-id], [data-action="open-event"][data-id], [data-action="open-reminder"][data-id]');
  if (!node) return null;
  const kind = node.dataset.kind || { 'open-task': 'TASK', 'open-event': 'EVENT', 'open-reminder': 'REMINDER' }[node.dataset.action];
  return kind ? { kind, id: node.dataset.id, node } : null;
}

export function findEntity(kind, id) {
  const lists = kind === 'TASK' ? [peek('/api/v1/tasks'), peek('/api/v1/today')?.tasks, peek('/api/v1/today')?.needs_refinement]
    : kind === 'EVENT' ? [peek('/api/v1/events'), peek('/api/v1/today')?.plan?.canonical_events, peek('/api/v1/plan/agenda?days=7')?.plan?.canonical_events]
      : [peek('/api/v1/reminders')];
  for (const list of lists) {
    const hit = (list || []).find((x) => x.id === id);
    if (hit) return hit;
  }
  return null;
}

// [{ id, label, icon, tone, run }]
export function quickActionsFor(kind, e) {
  const items = [];
  const add = (id, icon, run, tone = 'accent') => items.push({ id, label: t(`quick.${id}`), icon, tone, run });
  if (kind === 'TASK') {
    const place = taskPlace(e);
    if (place === 'open') {
      if (!e.started_at && e.status === 'ACTIVE') add('start', 'clock', () => change('task.start', e.id, {}, { success: t('today.started') }));
      add('complete', 'check', () => lifecycle(e.id, e.version, 'complete', { from: e.status }), 'ok');
      if (e.status === 'ACTIVE') add('progress', 'task', () => logProgress(e));
      add('reschedule', 'calendar', () => rescheduleSheet(e));
      add('remind', 'bell', () => remindAboutSheet({ ...e, kind: 'TASK' }));
      add('edit', 'settings', () => editTaskSheet(e));
      add('cancel', 'x', () => lifecycle(e.id, e.version, 'cancel', { title: e.title, from: e.status }), 'muted');
    } else if (place === 'done') {
      add('reopen', 'repeat', () => lifecycle(e.id, e.version, 'reopen', { from: e.status }));
      add('archive', 'evidence', () => archiveDone(e), 'muted');
    } else {
      add(e.status === 'ARCHIVED' && e.completed_at ? 'restoreDone' : 'restore', 'repeat',
        () => lifecycle(e.id, e.version, 'restore', { from: e.status }));
    }
    add('delete', 'x', () => lifecycle(e.id, e.version, 'delete', { title: e.title }), 'danger');
  } else if (kind === 'EVENT') {
    if (e.status === 'ACTIVE') {
      add('editEvent', 'calendar', () => eventSheet(e));
      add('remind', 'bell', () => remindAboutSheet({ ...e, kind: 'EVENT' }));
      add('cancelEvent', 'x', () => lifecycle(e.id, e.version, 'cancel', { title: e.title, kind: 'event' }), 'muted');
    } else {
      add('restoreEvent', 'repeat', () => lifecycle(e.id, e.version, 'reopen', { kind: 'event' }));
    }
    add('delete', 'x', () => lifecycle(e.id, e.version, 'delete', { title: e.title, kind: 'event' }), 'danger');
  } else if (kind === 'REMINDER') {
    if (isOpen(e)) {
      add('reminderDone', 'check', () => reminderAction(e, 'done'), 'ok');
      add('snooze', 'clock', () => snoozeChoices(e, (until) => reminderAction(e, 'snooze', { until })));
      add('editReminder', 'settings', () => reminderSheet(e));
      add('cancelReminder', 'x', () => reminderAction(e, 'cancel'), 'muted');
    } else {
      add('reopenReminder', 'repeat', () => reminderAction(e, 'reopen'));
    }
    add('delete', 'x', () => reminderAction(e, 'delete'), 'danger');
  }
  return items;
}

const quickFlights = new Set();

export async function singleFlightQuickAction(kind, id, run) {
  const key = `${kind}:${id}`;
  if (quickFlights.has(key)) return false;
  quickFlights.add(key);
  try {
    await run();
    return true;
  } finally {
    quickFlights.delete(key);
  }
}

export function openQuickActions(kind, id) {
  return singleFlightQuickAction(kind, id, async () => {
    const entity = findEntity(kind, id);
    if (!entity) return;
    const items = quickActionsFor(kind, entity);
    haptic('MEDIUM');
    const chosen = await actionSheet({ title: entity.title, items });
    const item = items.find((x) => x.id === chosen);
    if (item) await item.run();
  });
}

// A finished task swiped away: archive it at once, Undo for 10 seconds.
export function archiveDone(task) {
  return change('task.archive', task.id, {}, {
    success: t('quick.archived', { title: task.title }),
    undo: () => change('task.unarchive', task.id, {}),
  });
}

// ---- gestures -------------------------------------------------------------------

const LONG_PRESS_MS = 550;
// After the finger lifts, the browser may still send the click of that gesture.
const CLICK_AFTER_TOUCH_MS = 400;
// The row a gesture just used: the click the browser sends after it must not also open
// it. Only that row is affected; an Undo tapped right away still works.
let suppressOn = null;
let suppressTimer = null;
const suppress = (node, ms = null) => {
  clearTimeout(suppressTimer);
  suppressOn = node;
  suppressTimer = ms == null ? null : setTimeout(() => { if (suppressOn === node) suppressOn = null; }, ms);
};

export function installQuickActions(root = document, open = openQuickActions) {
  // One touch owns everything the browser derives from it: Android/WebView also
  // sends a contextmenu for the same press (before or after our timer, depending on
  // the system long-press timeout), and a click when the finger lifts. The sheet
  // opens once per gesture; those follow-ups are consumed.
  let gesture = null; // { ref, start, timer, fired }
  const fire = () => {
    if (!gesture || gesture.fired) return;
    clearTimeout(gesture.timer);
    gesture.fired = true;
    suppress(gesture.ref.node); // until the finger lifts (see touchend)
    open(gesture.ref.kind, gesture.ref.id);
  };
  const cancel = () => { if (gesture) clearTimeout(gesture.timer); gesture = null; };
  const consume = (event) => { event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation?.(); };

  // Right click (desktop), or the long press of a touch.
  root.addEventListener('contextmenu', (event) => {
    const ref = entityRef(event.target);
    if (!ref || event.target.closest('input,textarea,select')) return;
    if (gesture && gesture.ref.node === ref.node) { consume(event); fire(); return; }
    if (suppressOn && suppressOn.contains(event.target)) { consume(event); return; }
    event.preventDefault();
    open(ref.kind, ref.id);
  });
  // Long press (touch): a still finger for LONG_PRESS_MS.
  root.addEventListener('touchstart', (event) => {
    cancel();
    const ref = entityRef(event.target);
    if (!ref || event.touches.length !== 1) return;
    gesture = { ref, start: { x: event.touches[0].clientX, y: event.touches[0].clientY }, fired: false, timer: null };
    gesture.timer = setTimeout(fire, LONG_PRESS_MS);
  }, { passive: true });
  root.addEventListener('touchmove', (event) => {
    if (!gesture || gesture.fired) return;
    const dx = event.touches[0].clientX - gesture.start.x;
    const dy = event.touches[0].clientY - gesture.start.y;
    if (Math.hypot(dx, dy) > 10) cancel();
  }, { passive: true });
  const lift = () => {
    if (gesture?.fired) suppress(gesture.ref.node, CLICK_AFTER_TOUCH_MS);
    cancel();
  };
  root.addEventListener('touchend', lift);
  root.addEventListener('touchcancel', lift);
  root.addEventListener('click', (event) => {
    if (!suppressOn || !suppressOn.contains(event.target)) return;
    suppress(null);
    consume(event);
  }, true);
  installSwipeArchive(root);
}

// Swipe left on a finished task ([data-swipe="archive"]) to archive it. Only rows that
// opt in react; the Plan's day swipe lives on its own area and never sees these rows.
function installSwipeArchive(root) {
  let drag = null;
  root.addEventListener('touchstart', (event) => {
    const row = event.target.closest?.('[data-swipe="archive"]');
    if (!row || event.touches.length !== 1) return;
    drag = { row, x: event.touches[0].clientX, y: event.touches[0].clientY, dx: 0, horizontal: null };
  }, { passive: true });
  root.addEventListener('touchmove', (event) => {
    if (!drag) return;
    const dx = event.touches[0].clientX - drag.x;
    const dy = event.touches[0].clientY - drag.y;
    if (drag.horizontal == null && Math.hypot(dx, dy) > 8) drag.horizontal = Math.abs(dx) > Math.abs(dy) * 1.5;
    if (!drag.horizontal) return;
    drag.dx = Math.min(0, dx);
    drag.row.style.transform = `translateX(${drag.dx}px)`;
    drag.row.classList.toggle('swipe-armed', -drag.dx > drag.row.offsetWidth * 0.35);
  }, { passive: true });
  const end = () => {
    if (!drag) return;
    const { row, dx, horizontal } = drag;
    drag = null;
    if (!horizontal) return;
    suppress(row, 400);
    const armed = -dx > row.offsetWidth * 0.35;
    row.style.transition = 'transform .18s ease';
    row.style.transform = armed ? `translateX(-${row.offsetWidth}px)` : '';
    row.classList.remove('swipe-armed');
    if (!armed) { setTimeout(() => { row.style.transition = ''; }, 200); return; }
    const task = findEntity('TASK', row.dataset.id);
    setTimeout(() => (task ? archiveDone(task) : null), 180);
  };
  root.addEventListener('touchend', end);
  root.addEventListener('touchcancel', end);
}
