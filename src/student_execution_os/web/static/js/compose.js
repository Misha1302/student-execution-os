import { api } from './api.js';
import { t, code, fmtDuration, now } from './i18n.js';
import { esc, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, setBusy } from './ui.js';
import { mutate } from './actions.js';
import { openCapture } from './capture.js';
import { newEventSheet } from './events.js';

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
        ${chipGroup('duration', [[45, fmtDuration(45)], [60, fmtDuration(60)], [90, fmtDuration(90)], [120, fmtDuration(120)]], 90)}
      </div>
      <div class="field"><span>${esc(t('form.repeat'))}</span>
        ${chipGroup('freq', [['DAILY', t('form.repeat.daily')], ['WEEKLY', t('form.repeat.weekly')], ['WEEKLY2', t('form.repeat.biweekly')]], 'WEEKLY')}
      </div>
      <div class="field"><span>${esc(t('form.repeatCount'))}</span>
        ${chipGroup('count', [['', t('form.repeat.forever')], ['8', '8'], ['16', '16'], ['30', '30']], '16')}
      </div>
      <div class="field"><span>${esc(t('form.attendance'))}</span>
        ${chipGroup('attendance', ['REQUIRED', 'PREFERRED', 'OPTIONAL'].map((v) => [v, code('attendance', v)]), 'REQUIRED')}
      </div>
      <p class="help">${esc(t('form.timezone', { zone }))}</p>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('compose.create'))}</button>`,
  });
  const $f = (name) => dialog.querySelector(`[data-f="${name}"]`);
  setTimeout(() => $f('title').focus(), 80);
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const title = $f('title').value.trim();
    const local = $f('start').value;
    if (!title || !local) { toast(t('form.titleAndTime'), { error: true }); return; }
    const freq = chipValue(dialog, 'freq');
    const count = chipValue(dialog, 'count');
    const rule = [freq === 'DAILY' ? 'FREQ=DAILY' : 'FREQ=WEEKLY', freq === 'WEEKLY2' ? 'INTERVAL=2' : '', count ? `COUNT=${count}` : ''].filter(Boolean).join(';');
    setBusy(e.currentTarget, true);
    const created = await mutate(() => api('/api/v1/recurrence/templates', {
      method: 'POST',
      body: {
        title,
        dtstart_local: local.length === 16 ? `${local}:00` : local,
        duration_minutes: Number(chipValue(dialog, 'duration')),
        recurrence_rule: rule,
        timezone_name: zone,
        category: 'LESSON',
        attendance_policy: chipValue(dialog, 'attendance'),
        location_effect: { kind: 'NONE' },
      },
    }), { success: t('compose.recurringCreated') });
    setBusy(e.currentTarget, false);
    if (created) dialog.close('saved');
  });
}

// Tasks and events are captured in one flow (capture.js); forms remain for a blank
// event (events.js) and for a series.
export const composers = { task: () => openCapture(), event: () => newEventSheet(), recurring: recurringSheet };

export function compose() {
  return openCapture();
}
