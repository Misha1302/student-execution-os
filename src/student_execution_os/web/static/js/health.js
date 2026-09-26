// Notification health: can a reminder actually reach the user, and if not, what to
// do about it. The server knows whether its reminder worker runs, whether push is
// configured and which phones are registered (and what they last reported); only
// this device knows its own permission right now. Both are shown together, and the
// capture card uses the same answer so no screen promises a reminder that cannot
// arrive.
import { api } from './api.js';
import { load, peek } from './store.js';
import { t, fmtDateTime } from './i18n.js';
import { esc, icon, chip, toast, errorMessage, setBusy } from './ui.js';
import { deviceStatus, openDeviceSettings, alarmsSupported, testAlarm, isNative } from './native.js';

const HEALTH = '/api/v1/notifications/health';
let local = null;

export async function refreshLocalStatus() {
  local = await deviceStatus().catch(() => null);
  return local;
}

export const localStatus = () => local;

export async function loadHealth({ fresh = false } = {}) {
  const [server] = await Promise.all([load(HEALTH, { fresh }).catch((error) => ({ error })), refreshLocalStatus()]);
  return { server: server?.data || null, error: server?.error || null, local };
}

// What the product may promise for a reminder being set now.
//   OK            a phone will show it (and ring, for an alarm)
//   IN_APP        only visible inside the app
//   NONE          nothing will be sent (the reminder worker is down)
//   NO_ALARM      no phone can ring an alarm; it arrives as a notification
//   INEXACT_ALARM the alarm may ring a few minutes late
//   UNKNOWN       not known yet (offline, never checked)
export function reminderReach({ alarm = false } = {}) {
  const health = peek(HEALTH);
  if (local?.native && local.notifications === false) return { level: 'IN_APP', why: 'THIS_DEVICE_OFF' };
  if (!health) return { level: 'UNKNOWN', why: null };
  if (health.reach === 'NONE') return { level: 'NONE', why: health.problems?.find((p) => p.startsWith('WORKER_')) || 'WORKER_STALE' };
  if (health.reach === 'IN_APP') {
    const why = (health.problems || []).find((p) => ['PUSH_UNCONFIGURED', 'NO_DEVICE', 'DEVICE_NOTIFICATIONS_OFF'].includes(p)) || 'NO_DEVICE';
    return { level: 'IN_APP', why };
  }
  if (alarm && health.alarm === 'NONE') return { level: 'NO_ALARM', why: 'NO_ALARM_DEVICE' };
  if (alarm && health.alarm === 'INEXACT') return { level: 'INEXACT_ALARM', why: 'EXACT_ALARMS_OFF' };
  return { level: 'OK', why: null };
}

// A one-line warning for forms that set a reminder; empty when it will arrive.
export function reachWarning(options = {}) {
  const reach = reminderReach(options);
  if (reach.level === 'OK' || reach.level === 'UNKNOWN') return '';
  const tone = reach.level === 'NONE' ? 'danger' : 'warn';
  return `<div class="banner ${tone} reach-warning" data-reach="${esc(reach.level)}">${icon('bell')}<div>
    <p>${esc(t(`reach.${reach.level}`, { why: t(`health.problem.${reach.why}`) }))}</p>
    <button type="button" class="link" data-nav="settings" data-close-sheet>${esc(t('reach.check'))}</button></div></div>`;
}

function row(label, ok, detail = '', action = '') {
  const tone = ok === true ? 'ok' : ok === false ? 'danger' : 'muted';
  const text = ok === true ? t('health.ok') : ok === false ? t('health.off') : t('health.unknown');
  return `<div class="row static health-row"><span class="row-main"><strong>${esc(label)}</strong>${detail ? `<small>${esc(detail)}</small>` : ''}${action}</span>${chip(text, tone)}</div>`;
}

function fixButton(target) {
  return `<button type="button" class="button small" data-action="health-fix" data-target="${esc(target)}">${esc(t('health.fix'))}</button>`;
}

export function healthSection({ server, error, local: device } = {}) {
  const summary = !server ? '' : server.reach === 'PUSH'
    ? `<div class="banner ok">${icon('check')}<div><strong>${esc(t('health.summary.PUSH'))}</strong></div></div>`
    : `<div class="banner ${server.reach === 'NONE' ? 'danger' : 'warn'}">${icon('alert')}<div><strong>${esc(t(`health.summary.${server.reach}`))}</strong>
       <p>${esc((server.problems || []).map((p) => t(`health.problem.${p}`)).join(' · '))}</p></div></div>`;
  const rows = [];
  if (device?.native) {
    rows.push(row(t('health.thisPhone'), device.notifications ?? null, device.notifications === false ? t('health.thisPhoneOff') : '',
      device.notifications === false ? fixButton('notifications') : ''));
    if (alarmsSupported()) {
      rows.push(row(t('health.exactAlarms'), device.exact_alarms ?? null, device.exact_alarms === false ? t('health.exactAlarmsOff') : '',
        device.exact_alarms === false ? fixButton('exact_alarms') : ''));
      rows.push(row(t('health.fullScreen'), device.full_screen ?? null, device.full_screen === false ? t('health.fullScreenOff') : '',
        device.full_screen === false ? fixButton('full_screen') : ''));
      if (device.battery_optimized) rows.push(row(t('health.battery'), false, t('health.batteryHelp'), fixButton('battery')));
    }
  } else {
    rows.push(`<p class="help">${esc(t('health.browser'))}</p>`);
  }
  if (server) {
    rows.push(row(t('health.worker'), server.worker?.state === 'RUNNING', server.worker?.state === 'RUNNING'
      ? t('health.workerBeat', { when: fmtDateTime(server.worker.last_beat_at) }) : t(`health.problem.WORKER_${server.worker?.state || 'STALE'}`)));
    rows.push(row(t('health.push'), server.push_configured, server.push_configured ? '' : t('health.problem.PUSH_UNCONFIGURED')));
    const phones = server.devices || [];
    rows.push(row(t('health.phones'), phones.length > 0, phones.length
      ? phones.map((d) => `${d.label || 'Android'} · ${fmtDateTime(d.last_seen_at || d.updated_at)}${d.status?.notifications === false ? ` · ${t('health.off')}` : ''}`).join('; ')
      : t('health.problem.NO_DEVICE')));
    const recent = server.recent || {};
    rows.push(`<p class="help">${esc(t('health.recent', { sent: recent.sent || 0, missed: (recent.no_device || 0) + (recent.failed || 0) }))}${recent.last_sent_at ? ` ${esc(t('health.lastSent', { when: fmtDateTime(recent.last_sent_at) }))}` : ''}</p>`);
  } else if (error) {
    rows.push(`<p class="muted">${esc(errorMessage(error))}</p>`);
  }
  return `<section class="section" data-health>
      <div class="section-head"><h2>${esc(t('health.title'))}</h2></div>
      ${summary}
      <div class="card">${rows.join('')}
        <div class="button-row">
          <button class="button" data-action="health-test">${icon('bell')}${esc(t('health.test'))}</button>
          ${alarmsSupported() ? `<button class="button ghost" data-action="health-alarm">${esc(t('health.testAlarm'))}</button>` : ''}
        </div>
        <p class="help" data-health-result hidden></p>
      </div>
    </section>`;
}

const FINAL = new Set(['SENT', 'NO_DEVICE', 'DEAD', 'CANCELLED']);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function sendTest(button) {
  const out = button.closest('.card')?.querySelector('[data-health-result]');
  const say = (text, error = false) => { if (out) { out.hidden = false; out.textContent = text; out.classList.toggle('text-danger', error); } };
  setBusy(button, true);
  say(t('health.testSending'));
  try {
    let delivery = await api('/api/v1/notifications/test', { method: 'POST' });
    for (let i = 0; i < 14 && !FINAL.has(delivery.delivery_state); i += 1) {
      await sleep(1500);
      delivery = await api(`/api/v1/notifications/${encodeURIComponent(delivery.id)}/delivery`);
    }
    const state = delivery.delivery_state;
    const key = state === 'SENT' ? 'health.testSent'
      : state === 'NO_DEVICE' ? (delivery.last_error === 'PUSH_UNCONFIGURED' ? 'health.testUnconfigured' : 'health.testNoDevice')
        : state === 'DEAD' ? 'health.testFailed' : 'health.testStuck';
    say(t(key), state !== 'SENT');
  } catch (err) {
    say(errorMessage(err), true);
  } finally {
    setBusy(button, false);
  }
}

export const healthActions = {
  'health-test': (el) => sendTest(el),
  'health-fix': async (el, ctx) => {
    if (!(await openDeviceSettings(el.dataset.target))) toast(t('health.fixManual'), { error: true });
    // Coming back from the system screen: read the permissions again.
    setTimeout(() => ctx.refresh(), 1500);
  },
  'health-alarm': async () => {
    try {
      if (await testAlarm()) toast(t('health.alarmTesting'));
    } catch (err) { toast(errorMessage(err), { error: true }); }
  },
};

export const nativeHealthSupported = () => isNative();
