import { api } from './api.js';
import { t, code, fmtDuration, now } from './i18n.js';
import { esc, openSheet, actionSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, toast, setBusy } from './ui.js';
import { mutate } from './actions.js';

const EFFORTS = [15, 30, 60, 90, 120, 180];

function endOfDay(offsetDays) {
  const d = now();
  d.setDate(d.getDate() + offsetDays);
  d.setHours(23, 59, 0, 0);
  return d;
}

function nextHour() {
  const d = now();
  d.setMinutes(0, 0, 0);
  d.setHours(d.getHours() + 1);
  return d;
}

function field(label, control, hint = '') {
  return `<label class="field"><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
}

function taskSheet() {
  const dialog = openSheet({
    title: t('compose.task'),
    full: true,
    body: `<div class="form">
      ${field(t('form.title'), `<input data-f="title" maxlength="180" required placeholder="${esc(t('form.taskPlaceholder'))}" enterkeyhint="next">`)}
      <div class="field"><span>${esc(t('form.effort'))}</span>
        ${chipGroup('effort', [['unknown', t('form.effortUnknown')], ...EFFORTS.map((m) => [m, fmtDuration(m)]), ['custom', t('form.custom')]], 'unknown')}
        <input type="number" inputmode="numeric" min="5" step="5" data-f="effort" class="hidden" placeholder="${esc(t('form.minutes'))}">
      </div>
      <div class="field"><span>${esc(t('form.deadline'))}</span>
        ${chipGroup('deadline', [['UNKNOWN', t('form.deadline.unknown')], ['today', t('day.today')], ['tomorrow', t('day.tomorrow')], ['week', t('form.deadline.week')], ['exact', t('form.deadline.exact')], ['ABSENT', t('form.deadline.none')]], 'UNKNOWN')}
        <input type="datetime-local" data-f="cutoff" class="hidden">
        <small class="help" data-deadline-help>${esc(t('form.deadline.help.UNKNOWN'))}</small>
      </div>
      <div class="field"><span>${esc(t('form.importance'))}</span>
        ${chipGroup('importance', ['LOW', 'NORMAL', 'HIGH', 'CRITICAL'].map((v) => [v, code('importance', v)]), 'NORMAL')}
      </div>
      <details class="details">
        <summary>${esc(t('form.more'))}</summary>
        <div class="form">
          ${field(t('form.category'), `<select data-f="category">${['HOMEWORK', 'EXAM', 'LESSON', 'WORK', 'ADMIN', 'ERRAND', 'PERSONAL_APPOINTMENT', 'MEETING', 'GENERAL'].map((c) => `<option value="${c}" ${c === 'HOMEWORK' ? 'selected' : ''}>${esc(code('category', c))}</option>`).join('')}</select>`)}
          ${field(t('form.target'), `<input type="datetime-local" data-f="target">`, t('form.targetHelp'))}
          ${field(t('form.actionableFrom'), `<input type="datetime-local" data-f="actionable">`)}
          <div class="field"><span>${esc(t('form.split'))}</span>${chipGroup('split', [['false', t('form.split.no')], ['true', t('form.split.yes')]], 'false')}</div>
          <div class="field-row hidden" data-split-fields>
            ${field(t('form.minChunk'), `<input type="number" inputmode="numeric" min="5" step="5" data-f="min" value="30">`)}
            ${field(t('form.maxChunk'), `<input type="number" inputmode="numeric" min="5" step="5" data-f="max" value="90">`)}
          </div>
          ${field(t('form.description'), `<textarea data-f="description" rows="3" maxlength="2000"></textarea>`)}
        </div>
      </details>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('compose.create'))}</button>`,
  });
  const $f = (name) => dialog.querySelector(`[data-f="${name}"]`);
  dialog.addEventListener('chipchange', (e) => {
    if (e.detail.name === 'effort') $f('effort').classList.toggle('hidden', e.detail.value !== 'custom');
    if (e.detail.name === 'split') dialog.querySelector('[data-split-fields]').classList.toggle('hidden', e.detail.value !== 'true');
    if (e.detail.name === 'deadline') {
      const v = e.detail.value;
      $f('cutoff').classList.toggle('hidden', v !== 'exact');
      const helpKey = v === 'UNKNOWN' || v === 'ABSENT' ? v : 'KNOWN';
      dialog.querySelector('[data-deadline-help]').textContent = t(`form.deadline.help.${helpKey}`);
      if (v === 'exact' && !$f('cutoff').value) $f('cutoff').value = localInputValue(endOfDay(2));
    }
  });
  setTimeout(() => $f('title').focus(), 80);
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const title = $f('title').value.trim();
    if (!title) { $f('title').focus(); toast(t('form.titleRequired'), { error: true }); return; }
    const effortChoice = chipValue(dialog, 'effort');
    const effort = effortChoice === 'unknown' ? null : Number(effortChoice === 'custom' ? $f('effort').value : effortChoice);
    if (effortChoice !== 'unknown' && (!effort || effort <= 0)) { toast(t('form.effortRequired'), { error: true }); return; }
    const d = chipValue(dialog, 'deadline');
    let cutoff = { state: 'UNKNOWN' };
    if (d === 'ABSENT') cutoff = { state: 'ABSENT' };
    else if (d === 'today') cutoff = { state: 'KNOWN', at: endOfDay(0).toISOString() };
    else if (d === 'tomorrow') cutoff = { state: 'KNOWN', at: endOfDay(1).toISOString() };
    else if (d === 'week') cutoff = { state: 'KNOWN', at: endOfDay(7).toISOString() };
    else if (d === 'exact') {
      const at = isoFromLocalInput($f('cutoff').value);
      if (!at) { toast(t('form.deadlineRequired'), { error: true }); return; }
      cutoff = { state: 'KNOWN', at };
    }
    const splittable = chipValue(dialog, 'split') === 'true';
    const payload = {
      title,
      description: $f('description').value.trim() || null,
      category: $f('category').value,
      importance: chipValue(dialog, 'importance'),
      estimated_total_effort_minutes: effort,
      remaining_effort_minutes: effort,
      target_at: isoFromLocalInput($f('target').value),
      actionable_from: isoFromLocalInput($f('actionable').value),
      splittable,
      min_chunk_minutes: splittable ? Number($f('min').value) || null : null,
      max_chunk_minutes: splittable ? Number($f('max').value) || null : null,
      actual_cutoff: cutoff,
    };
    setBusy(e.currentTarget, true);
    const created = await mutate(() => api('/api/v1/tasks', { method: 'POST', body: payload }), { success: t('compose.taskCreated') });
    setBusy(e.currentTarget, false);
    if (created) dialog.close('saved');
  });
}

function eventSheet() {
  const start = nextHour();
  const dialog = openSheet({
    title: t('compose.event'),
    full: true,
    body: `<div class="form">
      ${field(t('form.title'), `<input data-f="title" maxlength="180" placeholder="${esc(t('form.eventPlaceholder'))}">`)}
      ${field(t('form.starts'), `<input type="datetime-local" data-f="start" value="${esc(localInputValue(start))}">`)}
      <div class="field"><span>${esc(t('form.duration'))}</span>
        ${chipGroup('duration', [[30, fmtDuration(30)], [60, fmtDuration(60)], [90, fmtDuration(90)], [120, fmtDuration(120)], [180, fmtDuration(180)]], 90)}
      </div>
      <div class="field"><span>${esc(t('form.attendance'))}</span>
        ${chipGroup('attendance', ['REQUIRED', 'PREFERRED', 'OPTIONAL'].map((v) => [v, code('attendance', v)]), 'REQUIRED')}
      </div>
      <div class="field"><span>${esc(t('form.location'))}</span>
        ${chipGroup('location', [['NONE', code('location', 'NONE')], ['REMOTE', code('location', 'REMOTE')]], 'NONE')}
        <small class="help">${esc(t('form.locationHelp'))}</small>
      </div>
      ${field(t('form.category'), `<select data-f="category">${['LESSON', 'EXAM', 'MEETING', 'WORK', 'PERSONAL_APPOINTMENT', 'GENERAL'].map((c) => `<option value="${c}">${esc(code('category', c))}</option>`).join('')}</select>`)}
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('compose.create'))}</button>`,
  });
  const $f = (name) => dialog.querySelector(`[data-f="${name}"]`);
  setTimeout(() => $f('title').focus(), 80);
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const title = $f('title').value.trim();
    const startsAt = isoFromLocalInput($f('start').value);
    if (!title || !startsAt) { toast(t('form.titleAndTime'), { error: true }); return; }
    const endsAt = new Date(new Date(startsAt).getTime() + Number(chipValue(dialog, 'duration')) * 60000).toISOString();
    setBusy(e.currentTarget, true);
    const created = await mutate(() => api('/api/v1/events', {
      method: 'POST',
      body: {
        title, starts_at: startsAt, ends_at: endsAt,
        category: $f('category').value,
        attendance_policy: chipValue(dialog, 'attendance'),
        location_effect: { kind: chipValue(dialog, 'location') },
      },
    }), { success: t('compose.eventCreated') });
    setBusy(e.currentTarget, false);
    if (created) dialog.close('saved');
  });
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

export const composers = { task: taskSheet, event: eventSheet, recurring: recurringSheet };

export async function compose() {
  const choice = await actionSheet({
    title: t('compose.title'),
    items: [
      { id: 'task', icon: 'task', label: t('compose.task'), hint: t('compose.taskHint') },
      { id: 'event', icon: 'event', label: t('compose.event'), hint: t('compose.eventHint'), tone: 'canonical' },
      { id: 'recurring', icon: 'repeat', label: t('compose.recurring'), hint: t('compose.recurringHint'), tone: 'canonical' },
    ],
  });
  if (choice) composers[choice]();
  return choice;
}
