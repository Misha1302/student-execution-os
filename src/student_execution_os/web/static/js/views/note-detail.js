import { load } from '../store.js';
import { apiBlob } from '../api.js';
import { t } from '../i18n.js';
import { esc, icon, chip, openSheet, confirmSheet, errorMessage } from '../ui.js';
import { change, shell } from '../actions.js';
import { newEntityId } from '../sync.js';
import { openCapture } from '../capture.js';

let currentNote = null;

function editor(note) {
  const dialog = openSheet({
    title: t('note.edit'), full: true,
    body: `<label class="field"><span>${esc(t('note.content'))}</span><textarea rows="10" maxlength="20000" data-content>${esc(note.content || '')}</textarea></label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const saved = await change('note.update', note.id, { expected_version: note.version, content: dialog.querySelector('[data-content]').value }, { success: t('note.saved') });
    if (saved) dialog.close('saved');
  });
}

function transcriptEditor(note) {
  const dialog = openSheet({
    title: t('note.transcript'), full: true,
    body: `<label class="field"><span>${esc(t('note.transcript'))}</span><textarea rows="10" maxlength="50000" data-transcript>${esc(note.transcript || '')}</textarea></label>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const value = dialog.querySelector('[data-transcript]').value.trim();
    if (!value) return;
    const saved = await change('note.transcript.set', note.id, { expected_version: note.version, transcript: value }, { success: t('note.saved') });
    if (saved) dialog.close('saved');
  });
}

export default {
  id: 'note', tab: 'more', detail: true, title: () => t('nav.notes'),
  async load({ fresh, params }) { return load(`/api/v1/notes/${encodeURIComponent(params[0])}`, { fresh }); },
  render(note) {
    currentNote = note;
    const body = note.content || note.transcript || t('note.voice');
    const transcriptState = note.transcription_state === 'FAILED' ? t('note.transcriptFailed')
      : note.transcript ? note.transcript : t('note.transcriptNone');
    return `<section class="section note-detail">
      <article class="card">
        <div class="task-top"><span class="kind-icon kind-task">${icon(note.audio ? 'mic' : 'note')}</span><span class="row-main">
          <strong>${esc((body || '').slice(0, 120) || t('note.voice'))}</strong><small>${esc(new Date(note.updated_at).toLocaleString())}</small></span>
          ${note._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
        </div>
        ${note.content ? `<p class="note-preview">${esc(note.content)}</p>` : ''}
        ${note.audio ? `<div class="field"><span>${esc(t('note.audioReady'))}</span><audio class="note-audio" controls data-note-audio></audio></div>` : ''}
        <div class="field"><span>${esc(t('note.transcript'))}</span><p class="muted note-preview">${esc(transcriptState)}</p></div>
        <div class="button-row">
          <button class="button" data-action="note-edit">${esc(t('note.edit'))}</button>
          ${note.audio ? `<button class="button ghost" data-action="note-transcript-edit">${esc(t('note.transcript'))}</button>` : ''}
        </div>
      </article>
      <section class="section">
        <div class="section-head"><h2>${esc(t('capture.title'))}</h2></div>
        <div class="card"><div class="button-row">
          <button class="button" data-action="note-task">${esc(t('note.convertTask'))}</button>
          <button class="button" data-action="note-event">${esc(t('note.convertEvent'))}</button>
          <button class="button ghost" data-action="note-project">${esc(t('note.convertProject'))}</button>
        </div></div>
      </section>
      <section class="section"><div class="card"><div class="button-row">
        <button class="button ghost" data-action="note-archive">${esc(t(note.lifecycle_status === 'ARCHIVED' ? 'note.unarchive' : 'note.archive'))}</button>
        <button class="button danger ghost" data-action="note-delete">${esc(t('note.delete'))}</button>
      </div></div></section>
    </section>`;
  },
  async mount(root, note) {
    if (!note.audio) return;
    const audio = root.querySelector('[data-note-audio]');
    try {
      const blob = await apiBlob(`/api/v1/notes/${encodeURIComponent(note.id)}/audio`);
      const url = URL.createObjectURL(blob);
      audio.src = url;
      audio.addEventListener('emptied', () => URL.revokeObjectURL(url), { once: true });
    } catch (err) {
      const p = document.createElement('p'); p.className = 'muted'; p.textContent = errorMessage(err); audio.replaceWith(p);
    }
  },
  actions: {
    'note-edit': () => editor(currentNote),
    'note-transcript-edit': () => transcriptEditor(currentNote),
    'note-task': () => openCapture({ text: currentNote.content || currentNote.transcript || '', sourceNoteId: currentNote.id, initialKind: 'TASK' }),
    'note-event': () => openCapture({ text: currentNote.content || currentNote.transcript || '', sourceNoteId: currentNote.id, initialKind: 'EVENT' }),
    'note-project': async () => {
      const source = (currentNote.content || currentNote.transcript || t('note.voice')).trim();
      const title = source.split(/\n/)[0].slice(0, 180) || t('note.voice');
      const id = newEntityId('project');
      const created = await change('project.create', id, { title });
      if (created) { await change('note.link', currentNote.id, { target_kind: 'PROJECT', target_id: id }); shell.go('project', { params: [id] }); }
    },
    'note-archive': async () => {
      const type = currentNote.lifecycle_status === 'ARCHIVED' ? 'note.unarchive' : 'note.archive';
      await change(type, currentNote.id, { expected_version: currentNote.version }, { success: t('note.saved') });
    },
    'note-delete': async () => {
      const ok = await confirmSheet({ title: t('note.deleteTitle'), body: `<p>${esc(t('note.deleteBody'))}</p>`, confirmLabel: t('note.delete'), danger: true });
      if (ok && await change('note.delete', currentNote.id, { expected_version: currentNote.version })) shell.go('notes');
    },
  },
};
