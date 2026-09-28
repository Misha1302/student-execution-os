import { t, code, fmtDuration, now } from './i18n.js';
import { esc, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, setBusy, focusSoon } from './ui.js';
import { durationPicker, takeDuration } from './duration.js';
import { change } from './actions.js';
import { newEntityId } from './sync.js';
import { openCapture } from './capture.js';
import { newEventSheet } from './events.js';
import { openAudioNoteRecorder } from './notes-audio.js';

function nextHour() {
  const d = now();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return d;
}

function field(label, control, hint = '') {
  return `<label class="field"><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
}

function recurringSheet() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const start = nextHour();
  const dialog = openSheet({
    title: t('compose.recurring'),
    full: true,
    body: `<div class="form">
      ${field(t('form.title'), `<input data-f="title" maxlength="180" placeholder="${esc(t('form.recurringPlaceholder'))}">`)}
      ${field(t('form.firstStart'), `<input type="datetime-local" data-f="start" value="${esc(localInputValue(start))}">`)}
      <div class="field"><span>${esc(t('form.duration'))}</span>
        ${durationPicker('duration', 90)}
      </div>
      <div class="field"><span>${esc(t('form.repeat'))}</span>
        ${chipGroup('freq', [['DAILY', t('form.repeat.daily')], ['WEEKLY', t('form.repeat.weekly')], ['WEEKLY2', t('form.repeat.biweekly')]], 'WEEKLY')}
      </div>
      <div class="field"><span>${esc(t('form.repeatCount'))}</span>
        ${chipGroup('count', [['', t('form.repeat.forever')], ['8', '8'], ['16', '16'], ['30', '30']], '16')}
      </div>
      ${field(t('series.location'), `<input data-f="location" maxlength="200" placeholder="${esc(t('series.locationPlaceholder'))}">`)}
      ${field(t('series.teacher'), `<input data-f="teacher" maxlength="200">`)}
      <div class="field"><span>${esc(t('form.attendance'))}</span>
        ${chipGroup('attendance', ['REQUIRED', 'PREFERRED', 'OPTIONAL'].map((v) => [v, code('attendance', v)]), 'REQUIRED')}
      </div>
      <p class="help">${esc(t('form.timezone', { zone }))}</p>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('compose.create'))}</button>`,
  });
  const $f = (name) => dialog.querySelector(`[data-f="${name}"]`);
  focusSoon(() => $f('title'));
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const title = $f('title').value.trim();
    const local = $f('start').value;
    if (!title || !local) { toast(t('form.titleAndTime'), { error: true }); return; }
    const duration = takeDuration(dialog, 'duration');
    if (!duration) return;
    const freq = chipValue(dialog, 'freq');
    const count = chipValue(dialog, 'count');
    const rule = [freq === 'DAILY' ? 'FREQ=DAILY' : 'FREQ=WEEKLY', freq === 'WEEKLY2' ? 'INTERVAL=2' : '', count ? `COUNT=${count}` : ''].filter(Boolean).join(';');
    setBusy(e.currentTarget, true);
    const payload = {
      title,
      dtstart_local: local.length === 16 ? `${local}:00` : local,
      duration_minutes: duration,
      recurrence_rule: rule,
      timezone_name: zone,
      category: 'LESSON',
      attendance_policy: chipValue(dialog, 'attendance'),
      location_effect: { kind: 'NONE' },
    };
    if ($f('location').value.trim()) payload.location_text = $f('location').value.trim();
    if ($f('teacher').value.trim()) payload.teacher = $f('teacher').value.trim();
    const created = await change('series.create', newEntityId('series'), payload, { success: t('compose.recurringCreated') });
    setBusy(e.currentTarget, false);
    if (created) dialog.close('saved');
  });
}

// Tasks and events are captured in one flow (capture.js); forms remain for a blank
// event (events.js) and for a series.
export const composers = { task: () => openCapture(), event: () => newEventSheet(), recurring: recurringSheet, 'note-audio': () => openAudioNoteRecorder() };

export function compose() {
  return openCapture();
}
