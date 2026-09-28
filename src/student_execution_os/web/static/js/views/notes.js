import { load } from '../store.js';
import { t } from '../i18n.js';
import { esc, icon, empty, sectionHead } from '../ui.js';
import { openCapture } from '../capture.js';
import { openAudioNoteRecorder } from '../notes-audio.js';

function row(note) {
  const text = (note.content || note.transcript || t('note.voice')).trim();
  return `<button class="row note-row ${note.lifecycle_status === 'ARCHIVED' ? 'archived' : ''}" data-action="open-note" data-id="${esc(note.id)}">
    <span class="row-icon tone-accent">${icon(note.audio ? 'mic' : 'note')}</span>
    <span class="row-main"><strong>${esc(text.slice(0, 180) || t('note.voice'))}</strong>
      <small>${esc(new Date(note.updated_at).toLocaleString())}</small></span>
    ${icon('chevron')}
  </button>`;
}

export default {
  id: 'notes', tab: 'more', title: () => t('nav.notes'),
  async load({ fresh }) { return load('/api/v1/notes?include_archived=true', { fresh }); },
  render(data) {
    const active = (data || []).filter((n) => n.lifecycle_status === 'ACTIVE');
    const archived = (data || []).filter((n) => n.lifecycle_status === 'ARCHIVED');
    return `<section class="section">
      <div class="button-row notes-new">
        <button type="button" class="button primary" data-new-note>${icon('note')} ${esc(t('notes.new'))}</button>
        <button type="button" class="button" data-new-voice-note>${icon('mic')} ${esc(t('note.recordAudio'))}</button>
      </div>
      <label class="search">${icon('search')}<input type="search" data-note-search placeholder="${esc(t('notes.search'))}"></label>
      <div data-note-list>
        ${sectionHead(t('notes.active'))}
        ${active.length ? `<div class="list">${active.map(row).join('')}</div>` : empty(t('notes.empty'), t('notes.emptyHint'), 'note')}
        ${archived.length ? `<div class="section-head"><h2>${esc(t('notes.archive'))}</h2></div><div class="list">${archived.map(row).join('')}</div>` : ''}
      </div>
    </section>`;
  },
  mount(root, data) {
    root.querySelector('[data-new-note]')?.addEventListener('click', () => openCapture({ initialKind: 'NOTE' }));
    root.querySelector('[data-new-voice-note]')?.addEventListener('click', () => openAudioNoteRecorder());
    const input = root.querySelector('[data-note-search]');
    const list = root.querySelector('[data-note-list]');
    input?.addEventListener('input', () => {
      const q = input.value.trim().toLocaleLowerCase();
      const found = (data || []).filter((n) => !q || `${n.content || ''} ${n.transcript || ''}`.toLocaleLowerCase().includes(q));
      list.innerHTML = found.length ? `<div class="list">${found.map(row).join('')}</div>` : `<p class="muted pad">${esc(t('tasks.noMatch'))}</p>`;
    });
  },
};
