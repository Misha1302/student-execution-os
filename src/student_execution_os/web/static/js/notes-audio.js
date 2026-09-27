import { api, apiUpload } from './api.js';
import { change, shell } from './actions.js';
import { flushSync, newEntityId, settled } from './sync.js';
import { t } from './i18n.js';
import { esc, openSheet, toast, errorMessage, setBusy } from './ui.js';

export function openAudioNoteRecorder(existing = null) {
  const noteId = existing?.id || newEntityId('note');
  let queued = Boolean(existing);
  const dialog = openSheet({
    title: t('note.recordAudio'), full: true,
    body: `<div class="form">
      <label class="field"><span>${esc(t('note.chooseAudio'))}</span>
        <input type="file" accept="audio/*" capture data-audio>
        <small class="help">${esc(t('note.chooseAudioHelp'))}</small>
      </label>
      <label class="field"><span>${esc(t('note.transcriptOptional'))}</span>
        <textarea rows="5" maxlength="50000" data-transcript>${esc(existing?.transcript || '')}</textarea>
      </label>
      <p class="help" data-status></p>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  const input = dialog.querySelector('[data-audio]');
  const transcript = dialog.querySelector('[data-transcript]');
  const status = dialog.querySelector('[data-status]');
  dialog.querySelector('[data-save]').addEventListener('click', async (event) => {
    const file = input.files?.[0];
    if (!file) { status.textContent = t('note.chooseAudioFirst'); return; }
    if (!file.type.startsWith('audio/')) { status.textContent = t('note.audioOnly'); return; }
    if (file.size > 25 * 1024 * 1024) { status.textContent = t('note.audioTooLarge'); return; }
    setBusy(event.currentTarget, true);
    try {
      if (!queued) {
        const op = await change('note.create', noteId, { content: '', source_kind: 'VOICE' });
        if (!op) return;
        queued = true;
        await flushSync().catch(() => {});
        const state = await settled(op.op_id, 2500);
        if (state === 'PENDING') {
          status.textContent = t('note.audioOffline');
          setBusy(event.currentTarget, false);
          return;
        }
      } else {
        await flushSync().catch(() => {});
      }
      let note = await api(`/api/v1/notes/${encodeURIComponent(noteId)}`);
      if (!note.audio) {
        note = await apiUpload(`/api/v1/notes/${encodeURIComponent(noteId)}/audio`, file, {
          mimeType: file.type || 'audio/webm', filename: file.name || 'voice-note',
        });
      }
      const text = transcript.value.trim();
      if (text && text !== (note.transcript || '')) {
        await change('note.transcript.set', noteId, { expected_version: note.version, transcript: text });
        await flushSync().catch(() => {});
      }
      dialog.close('saved');
      toast(t('note.audioSaved'), {
        action: { label: t('common.open'), run: () => shell.go('note', { params: [noteId] }) },
      });
    } catch (err) {
      status.textContent = err?.code === 'NETWORK' ? t('note.audioOffline') : errorMessage(err);
      setBusy(event.currentTarget, false);
    }
  });
  return dialog;
}
