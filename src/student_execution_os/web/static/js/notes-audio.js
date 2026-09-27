import { api, apiUpload } from './api.js';
import { change, shell } from './actions.js';
import { flushSync, newEntityId, settled } from './sync.js';
import { t } from './i18n.js';
import { esc, openSheet, toast, errorMessage, setBusy } from './ui.js';

function extensionFor(mime) {
  if (mime.includes('mp4') || mime.includes('m4a')) return 'm4a';
  if (mime.includes('ogg')) return 'ogg';
  if (mime.includes('wav')) return 'wav';
  return 'webm';
}

export function openAudioNoteRecorder(existing = null) {
  const noteId = existing?.id || newEntityId('note');
  let queued = Boolean(existing);
  let stream = null;
  let recorder = null;
  let chunks = [];
  let recordedBlob = null;
  let discardRecording = false;

  const canRecord = Boolean(globalThis.MediaRecorder && navigator.mediaDevices?.getUserMedia);
  const dialog = openSheet({
    title: t('note.recordAudio'), full: true,
    body: `<div class="form">
      ${canRecord ? `<div class="card">
        <div class="voice-recorder-state" data-record-state="idle"><strong data-record-label>${esc(t('note.recordReady'))}</strong></div>
        <div class="button-row">
          <button type="button" class="button primary" data-record-start>${esc(t('note.recordStart'))}</button>
          <button type="button" class="button danger" data-record-stop hidden>${esc(t('note.recordStop'))}</button>
          <button type="button" class="button ghost" data-record-cancel hidden>${esc(t('note.recordCancel'))}</button>
        </div>
      </div>
      <p class="help">${esc(t('note.orChooseAudio'))}</p>` : ''}
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
  const start = dialog.querySelector('[data-record-start]');
  const stop = dialog.querySelector('[data-record-stop]');
  const cancel = dialog.querySelector('[data-record-cancel]');
  const recordState = dialog.querySelector('[data-record-state]');
  const recordLabel = dialog.querySelector('[data-record-label]');

  function stopTracks() {
    stream?.getTracks?.().forEach((track) => track.stop());
    stream = null;
  }

  function setRecordState(state, labelKey) {
    if (!recordState) return;
    recordState.dataset.recordState = state;
    recordLabel.textContent = t(labelKey);
    start.hidden = state === 'recording';
    stop.hidden = state !== 'recording';
    cancel.hidden = state === 'idle';
  }

  start?.addEventListener('click', async () => {
    status.textContent = '';
    discardRecording = false;
    recordedBlob = null;
    chunks = [];
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
        .find((candidate) => MediaRecorder.isTypeSupported?.(candidate));
      recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      recorder.addEventListener('dataavailable', (event) => {
        if (event.data?.size) chunks.push(event.data);
      });
      recorder.addEventListener('stop', () => {
        stopTracks();
        if (discardRecording) {
          chunks = [];
          recordedBlob = null;
          setRecordState('idle', 'note.recordReady');
          return;
        }
        recordedBlob = new Blob(chunks, { type: recorder.mimeType || chunks[0]?.type || 'audio/webm' });
        setRecordState(recordedBlob.size ? 'ready' : 'idle', recordedBlob.size ? 'note.recorded' : 'note.recordReady');
      }, { once: true });
      recorder.start(500);
      setRecordState('recording', 'note.recording');
    } catch (err) {
      stopTracks();
      status.textContent = err?.name === 'NotAllowedError' ? t('ask.voiceDenied') : errorMessage(err);
    }
  });

  stop?.addEventListener('click', () => {
    if (recorder?.state === 'recording') recorder.stop();
  });

  cancel?.addEventListener('click', () => {
    if (recorder?.state === 'recording') {
      discardRecording = true;
      recorder.stop();
    } else {
      recordedBlob = null;
      chunks = [];
      setRecordState('idle', 'note.recordReady');
    }
  });

  input.addEventListener('change', () => {
    if (input.files?.length) {
      recordedBlob = null;
      if (recordState) setRecordState('idle', 'note.recordReady');
    }
  });

  dialog.querySelector('[data-save]').addEventListener('click', async (event) => {
    const picked = input.files?.[0] || null;
    const blob = recordedBlob || picked;
    if (!blob) { status.textContent = t('note.chooseAudioFirst'); return; }
    if (picked && !picked.type.startsWith('audio/')) { status.textContent = t('note.audioOnly'); return; }
    if (blob.size > 25 * 1024 * 1024) { status.textContent = t('note.audioTooLarge'); return; }
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
        const mimeType = blob.type || 'audio/webm';
        const filename = picked?.name || `voice-note.${extensionFor(mimeType)}`;
        note = await apiUpload(`/api/v1/notes/${encodeURIComponent(noteId)}/audio`, blob, { mimeType, filename });
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

  dialog.addEventListener('close', () => {
    if (recorder?.state === 'recording') {
      discardRecording = true;
      recorder.stop();
    } else stopTracks();
  }, { once: true });
  return dialog;
}
