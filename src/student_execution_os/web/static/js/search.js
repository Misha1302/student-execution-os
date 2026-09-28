// Global search over tasks, events, reminders and Notes, from the
// device's cached lists, so it also works offline. Same projection as «Дела».
import { load, peek } from './store.js';
import { t, now } from './i18n.js';
import { esc, openSheet, icon, focusSoon } from './ui.js';
import { commitments } from './agenda.js';
import { commitmentRow } from './views/tasks.js';

async function lists() {
  const get = async (path) => peek(path) || (await load(path, { cached: true }).catch(() => load(path).catch(() => ({ data: [] })))).data || [];
  const [tasks, events, reminders, notes] = await Promise.all([get('/api/v1/tasks'), get('/api/v1/events'), get('/api/v1/reminders'), get('/api/v1/notes')]);
  return { tasks, events, reminders, notes };
}

export async function openSearch() {
  const data = await lists();
  const dialog = openSheet({
    title: t('search.title'),
    full: true,
    body: `<label class="search">${icon('search')}<input type="search" data-q placeholder="${esc(t('agenda.search'))}" enterkeyhint="search"></label>
      <div data-results class="task-list"></div>`,
  });
  const input = dialog.querySelector('[data-q]');
  const results = dialog.querySelector('[data-results]');
  const draw = () => {
    const q = input.value.trim();
    if (!q) { results.innerHTML = `<p class="help pad">${esc(t('search.hint'))}</p>`; return; }
    const found = commitments(data, { now: now(), query: q }).slice(0, 40);
    const needle = q.toLocaleLowerCase();
    const noteRows = (data.notes || []).filter((n) => `${n.content || ''} ${n.transcript || ''}`.toLocaleLowerCase().includes(needle)).slice(0, 20)
      .map((n) => `<button class="row" data-action="open-note" data-id="${esc(n.id)}"><span class="row-icon tone-accent">${icon(n.audio ? 'mic' : 'note')}</span><span class="row-main"><strong>${esc((n.content || n.transcript || t('note.voice')).slice(0, 140))}</strong><small>${esc(t('nav.notes'))}</small></span>${icon('chevron')}</button>`);
    results.innerHTML = found.length || noteRows.length ? found.map(commitmentRow).join('') + noteRows.join('') : `<p class="muted pad">${esc(t('tasks.noMatch'))}</p>`;
  };
  input.addEventListener('input', draw);
  // Opening a result closes the search.
  results.addEventListener('click', (e) => { if (e.target.closest('[data-action]')) setTimeout(() => dialog.close('open'), 0); });
  draw();
  focusSoon(input, 60);
}
