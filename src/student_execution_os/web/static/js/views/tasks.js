import { load } from '../store.js';
import { t, code, fmtDuration, fmtDateTime, fmtRelative, now } from '../i18n.js';
import { esc, icon, chip, riskChip, empty, chipGroup } from '../ui.js';

const RISKY = new Set(['START_SOON', 'AT_RISK', 'CRITICAL', 'IMPOSSIBLE', 'OVERDUE']);
const FILTERS = {
  active: (x) => x.status === 'ACTIVE' || x.status === 'DRAFT',
  risk: (x) => x.status === 'ACTIVE' && RISKY.has(x.risk?.state),
  done: (x) => x.status === 'COMPLETED',
  // "Не буду делать" and archived tasks: out of the plan, still restorable.
  archive: (x) => x.status === 'CANCELLED' || x.status === 'ARCHIVED',
};
const RISK_ORDER = ['OVERDUE', 'IMPOSSIBLE', 'CRITICAL', 'AT_RISK', 'START_SOON', 'UNKNOWN', 'SAFE', 'NOT_APPLICABLE'];

let filter = 'active';
let query = '';

export function deadlineOf(task) {
  if (task.actual_cutoff?.state === 'KNOWN' && task.actual_cutoff.at) return { at: task.actual_cutoff.at, kind: 'cutoff' };
  if (task.target_at) return { at: task.target_at, kind: 'target' };
  return null;
}

function sortTasks(list) {
  const rank = (x) => RISK_ORDER.indexOf(x.risk?.state || 'UNKNOWN');
  const due = (x) => { const d = deadlineOf(x); return d ? new Date(d.at).getTime() : Infinity; };
  if (filter === 'done') return [...list].sort((a, b) => new Date(b.completed_at || 0) - new Date(a.completed_at || 0));
  return [...list].sort((a, b) => rank(a) - rank(b) || due(a) - due(b) || a.title.localeCompare(b.title));
}

// Progress as a share: counted items when the task has them ("3 из 10"), time otherwise.
export function progressOf(x) {
  const count = x.count_progress;
  if (count?.total) return { pct: Math.round((count.done / count.total) * 100), label: `${count.done}/${count.total}${count.unit ? ` ${count.unit}` : ''}` };
  const total = Number(x.estimated_total_effort_minutes || 0);
  const left = Number(x.remaining_effort_minutes || 0);
  const pct = total ? Math.round(((total - left) / total) * 100) : 0;
  return { pct, label: pct ? `${pct}%` : '' };
}

export function taskCard(x) {
  const d = deadlineOf(x);
  const left = Number(x.remaining_effort_minutes || 0);
  const { pct, label } = progressOf(x);
  const overdue = d && new Date(d.at) < now() && x.status === 'ACTIVE';
  return `<button class="task-card" data-action="open-task" data-id="${esc(x.id)}">
    <span class="task-top">
      <strong class="task-title">${esc(x.title)}</strong>
      ${x.status === 'ACTIVE' ? riskChip(x.risk) : chip(code('status', x.status), x.status === 'COMPLETED' ? 'ok' : x.status === 'DRAFT' ? 'warn' : 'muted')}
    </span>
    <span class="task-meta">
      ${d ? `<span class="${overdue ? 'text-danger' : ''}">${icon(d.kind === 'cutoff' ? 'flag' : 'clock')}${esc(fmtDateTime(d.at))} · ${esc(fmtRelative(d.at))}</span>` : `<span class="muted">${icon('flag')}${esc(code('cutoffState', x.actual_cutoff?.state))}</span>`}
      <span>${x.status === 'COMPLETED' && x.completed_at ? esc(t('tasks.doneAt', { when: fmtDateTime(x.completed_at) })) : x.estimated_total_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(t('tasks.left', { d: fmtDuration(left) }))}</span>
    </span>
    <span class="progress-row"><span class="progress" aria-hidden="true"><span data-w="${pct}"></span></span>${label ? `<small>${esc(label)}</small>` : ''}${x._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}</span>
  </button>`;
}

export default {
  id: 'tasks',
  tab: 'tasks',
  title: () => t('nav.tasks'),
  load: ({ fresh }) => load('/api/v1/tasks', { fresh }),
  render(tasks) {
    const counts = Object.fromEntries(Object.entries(FILTERS).map(([k, fn]) => [k, tasks.filter(fn).length]));
    const q = query.trim().toLowerCase();
    const list = sortTasks(tasks.filter(FILTERS[filter]).filter((x) => !q || x.title.toLowerCase().includes(q)));
    const segments = Object.keys(FILTERS).map((k) => [k, `${t(`tasks.filter.${k}`)} ${counts[k] ? counts[k] : ''}`.trim()]);
    let body;
    if (!tasks.length) body = empty(t('tasks.emptyAll'), t('tasks.emptyAllHint'), 'tasks') + `<button class="button primary wide" data-action="compose-task">${icon('plus')}${esc(t('capture.title'))}</button>`;
    else if (!list.length) body = empty(q ? t('tasks.noMatch') : t(`tasks.empty.${filter}`), '', filter === 'risk' ? 'check' : 'tasks');
    else body = `<div class="task-list">${list.map(taskCard).join('')}</div>`;
    return `
      <div class="toolbar">
        <label class="search">${icon('search')}<input type="search" data-task-search placeholder="${esc(t('tasks.search'))}" value="${esc(query)}" enterkeyhint="search"></label>
        <div class="segmented">${chipGroup('task-filter', segments, filter)}</div>
      </div>
      <section class="section">${body}</section>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name !== 'task-filter') return;
      filter = e.detail.value;
      ctx.rerender();
    });
    const input = root.querySelector('[data-task-search]');
    input?.addEventListener('input', () => {
      query = input.value;
      const pos = input.selectionStart;
      ctx.rerender().then(() => {
        const next = document.querySelector('[data-task-search]');
        next?.focus();
        next?.setSelectionRange(pos, pos);
      });
    });
  },
};
