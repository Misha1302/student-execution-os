import { load, clearAll, invalidate } from '../store.js';
import { api, session, clearAuth } from '../api.js';
import { t, code, fmtDateTime, getLocale, setLocale, LOCALES } from '../i18n.js';
import { esc, icon, chip, kv, openSheet, chipGroup, toast, errorMessage, setBusy, confirmSheet } from '../ui.js';
import { isNative, saveJson, prefGet, prefSet } from '../native.js';
import { getTheme, setTheme } from '../theme.js';
import { shell } from '../actions.js';
import { aiSection, aiActions, loadAiSettings } from '../ai.js';
import { healthSection, healthActions, loadHealth } from '../health.js';
import { syncSection, syncActions, loadConnectors } from '../sync-panel.js';
import { appUpdateService, UpdateChannel, UpdateState } from '../update-service.js';

async function loadUpdates() {
  if (!isNative()) return null;
  await appUpdateService.initialize();
  return { state: appUpdateService.getState(), preferences: await appUpdateService.preferences() };
}

function updateSection(updates) {
  if (!updates) return '';
  const { state, preferences } = updates;
  const target = state.target;
  const locale = getLocale();
  const releaseNotes = target?.release?.releaseNotes?.[locale] || target?.release?.releaseNotes?.en;
  const status = t(`updates.state.${state.status}`);
  const percent = Math.round((state.progress || 0) * 100);
  const progress = `<div data-update-progress ${state.status === UpdateState.DOWNLOADING ? '' : 'hidden'}>
    <div class="progress"><span data-w="${percent}" data-update-progress-bar></span></div>
    <small data-update-progress-label>${esc(t('updates.progress', { n: percent }))}</small>
  </div>`;
  const error = state.error ? `<div class="banner danger">${icon('alert')}<div><strong>${esc(t(`updates.error.${state.error.code}`))}</strong><p>${esc(state.error.message || '')}</p></div></div>` : '';
  const notes = releaseNotes ? `<div class="update-notes"><strong>${esc(releaseNotes.summary)}</strong><ul>${releaseNotes.changes.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div>` : '';
  const available = [UpdateState.AVAILABLE, UpdateState.FAILED].includes(state.status) && target && !state.downloaded
    ? `<button class="button primary" data-action="update-download">${esc(t('updates.download'))}</button>` : '';
  const ready = [UpdateState.READY_TO_INSTALL, UpdateState.FAILED].includes(state.status) && state.downloaded
    ? `<button class="button primary" data-action="update-apply">${esc(t('updates.restartInstall'))}</button>` : '';
  const permission = state.error?.code === 'PERMISSION_REQUIRED'
    ? `<button class="button" data-action="update-permission">${esc(t('updates.allowInstall'))}</button>` : '';
  return `<section class="section">
    <div class="section-head"><h2>${esc(t('updates.title'))}</h2></div>
    <div class="card form">
      <dl class="kv-list">
        ${kv(t('updates.version'), state.currentVersion || '—')}
        ${kv(t('updates.channel'), t(`updates.channel.${preferences.channel}`))}
        ${target ? kv(t('updates.availableVersion'), target.release.version.toString()) : ''}
        ${kv(t('updates.status'), status)}
      </dl>
      ${preferences.betaChannelAvailable ? `<div class="field"><span>${esc(t('updates.channel'))}</span>${chipGroup('update-channel', [[UpdateChannel.STABLE, t('updates.channel.STABLE')], [UpdateChannel.BETA, t('updates.channel.BETA')]], preferences.channel)}
        ${preferences.channel === UpdateChannel.BETA ? `<small class="help text-danger">${esc(t('updates.betaWarning'))}</small>` : ''}</div>` : ''}
      <label class="setting-toggle"><span><strong>${esc(t('updates.autoCheck'))}</strong></span><input type="checkbox" data-update-pref="autoCheck" ${preferences.autoCheck ? 'checked' : ''}></label>
      <label class="setting-toggle"><span><strong>${esc(t('updates.autoDownload'))}</strong></span><input type="checkbox" data-update-pref="autoDownload" ${preferences.autoDownload ? 'checked' : ''}></label>
      ${target && target.mandatory !== 'OPTIONAL' ? `<div class="banner danger">${icon('alert')}<div><strong>${esc(t(`updates.mandatory.${target.mandatory}`))}</strong><p>${esc(t('updates.dataSafe'))}</p></div></div>` : ''}
      ${notes}${progress}${error}
      <div class="button-row"><button class="button" data-action="update-check">${esc(t('updates.check'))}</button>${available}${ready}${permission}</div>
      ${!state.enabled ? `<p class="help">${esc(t('updates.notConfigured'))}</p>` : ''}
    </div>
  </section>`;
}

export async function logout() {
  if (session.authMode === 'session') {
    try {
      const device = JSON.parse((await prefGet('seos.pushDevice')) || 'null');
      if (device) await api(`/api/v1/mobile/devices/${encodeURIComponent(device.id)}/revoke`, { method: 'POST', body: { expected_version: device.version } });
      await prefSet('seos.pushDevice', null);
    } catch { /* best effort; account deletion also cascades device tokens */ }
    try { await api('/api/v1/auth/logout', { method: 'POST' }); } catch { /* token is dropped locally anyway */ }
  }
  await clearAuth();
  clearAll();
  shell.go('welcome');
}

async function exportAccount(button) {
  setBusy(button, true);
  try {
    const data = await api('/api/v1/account/export');
    await saveJson('student-execution-os-export.json', JSON.stringify(data, null, 2));
  } catch (err) {
    toast(errorMessage(err), { error: true });
  } finally {
    setBusy(button, false);
  }
}

function deleteSheet(policy) {
  const sessionMode = session.authMode === 'session';
  const expected = sessionMode ? session.user?.login : policy.account_id;
  const dialog = openSheet({
    title: t('settings.deleteTitle'),
    body: `<div class="banner danger">${icon('alert')}<div><strong>${esc(t('settings.deleteNow'))}</strong><p>${esc(t('settings.deleteWhat'))}</p></div></div>
      <dl class="kv-list">
        ${kv(sessionMode ? t('settings.login') : t('settings.accountId'), expected)}
        ${kv(t('settings.tombstone'), t('settings.days', { n: policy.tombstone_retention_days }))}
      </dl>
      <label class="field"><span>${esc(sessionMode ? t('settings.typeLogin') : t('settings.typeAccount'))}</span>
        <input id="account-delete-confirm" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${esc(expected)}"></label>
      <p class="help">${esc(t('settings.tombstoneHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('settings.keepAccount'))}</button>
      <button type="button" class="button danger" data-confirm>${esc(t('settings.deleteConfirm'))}</button>`,
  });
  dialog.querySelector('[data-confirm]').addEventListener('click', async (e) => {
    const typed = dialog.querySelector('#account-delete-confirm').value.trim();
    const body = { expected_server_revision: Number(policy.server_revision) };
    if (sessionMode) body.confirm_login = typed; else body.confirm_account_id = typed;
    setBusy(e.currentTarget, true);
    try {
      await api('/api/v1/account/delete', { method: 'POST', body });
      dialog.close('deleted');
      toast(t('settings.deleted'));
      await clearAuth();
      clearAll();
      shell.go(sessionMode ? 'welcome' : 'today');
    } catch (err) {
      toast(errorMessage(err), { error: true });
      setBusy(e.currentTarget, false);
    }
  });
}

// One "I sleep from … to …" setting drives both halves of the product: reminders
// stay quiet then (unless the user asked for that exact time) and the planner does
// not put work there.
export function wakingWindows(from, to) {
  const [fh, fm] = from.split(':').map(Number);
  const [th, tm] = to.split(':').map(Number);
  const sleep = fh * 60 + fm;
  const wake = th * 60 + tm;
  const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
  let day;
  if (sleep === wake) throw new Error(t('settings.sleepSame'));
  if (wake < sleep) day = [[hhmm(wake), hhmm(Math.min(sleep, 23 * 60 + 59))]]; // 23:00 → 07:00
  else day = [...(sleep > 0 ? [['00:00', hhmm(sleep)]] : []), [hhmm(wake), '23:59']]; // 01:00 → 09:00
  return Object.fromEntries(['1', '2', '3', '4', '5', '6', '7'].map((d) => [d, day]));
}

async function saveSleep(button, ctx) {
  const root = button.closest('.card');
  const from = root.querySelector('[data-f="sleep-from"]').value;
  const to = root.querySelector('[data-f="sleep-to"]').value;
  if (!from || !to) return;
  let windows;
  try { windows = wakingWindows(from, to); } catch (err) { toast(err.message, { error: true }); return; }
  setBusy(button, true);
  try {
    await api('/api/v1/notification-preferences', { method: 'PATCH', body: { quiet_hours: { starts_local: from, ends_local: to } } });
    const profile = await api('/api/v1/settings/planning-profile');
    await api('/api/v1/settings/planning-profile', { method: 'PATCH', body: { expected_version: profile.version, planning_windows: windows } });
    invalidate();
    toast(t('settings.sleepSaved', { from, to }));
    ctx.refresh();
  } catch (err) {
    toast(errorMessage(err), { error: true });
  } finally {
    setBusy(button, false);
  }
}

export default {
  id: 'settings',
  tab: 'more',
  detail: true,
  title: () => t('nav.settings'),
  async load({ fresh }) {
    const [diag, deletion, prefs, llm, profile, health, connectors, updates] = await Promise.all([
      load('/api/v1/settings/diagnostics', { fresh }),
      load('/api/v1/account/deletion-policy', { fresh }),
      load('/api/v1/notification-preferences', { fresh }).catch(() => ({ data: null })),
      loadAiSettings(), // not cached on the device
      load('/api/v1/settings/planning-profile', { fresh }).catch(() => ({ data: null })),
      loadHealth({ fresh }),
      loadConnectors(),
      loadUpdates(),
    ]);
    return { data: { diag: diag.data, deletion: deletion.data, prefs: prefs.data, llm, profile: profile.data, health, connectors, updates }, stale: diag.stale, fetchedAt: diag.fetchedAt };
  },
  render({ diag, deletion, prefs, llm, profile, health, connectors, updates }) {
    this._deletion = deletion;
    this._prefs = prefs;
    this._profile = profile;
    const level = prefs ? (prefs.enabled ? prefs.intensity : 'OFF') : 'OFF';
    this._llm = llm;
    const sessionMode = session.authMode === 'session';
    return `
      <section class="section">
        <div class="section-head"><h2>${esc(t('settings.account'))}</h2></div>
        <div class="card">
          <div class="account-row">
            <span class="avatar">${esc((session.user?.login || 'U').slice(0, 1).toUpperCase())}</span>
            <div class="row-main"><strong>${esc(sessionMode ? session.user?.login : t('settings.localAccount'))}</strong></div>
          </div>
          ${sessionMode ? `<div class="button-row">
            <button class="button ghost" data-action="logout">${icon('logout')}${esc(t('settings.logout'))}</button>
          </div>` : ''}
        </div>
      </section>

      ${prefs ? `<section class="section">
        <div class="section-head"><h2>${esc(t('settings.reminders'))}</h2></div>
        <div class="card form">
          <div class="field"><span>${esc(t('settings.reminderStyle'))}</span>${chipGroup('intensity', [['OFF', t('settings.remind.OFF')], ['GENTLE', t('settings.remind.GENTLE')], ['NORMAL', t('settings.remind.NORMAL')], ['PERSISTENT', t('settings.remind.PERSISTENT')]], level)}</div>
          <p class="help" data-intensity-help>${esc(t(`settings.remindHelp.${level}`))}</p>
          <hr>
          <div class="field"><span>${esc(t('settings.sleep'))}</span>
            <div class="field-row">
              <label class="field"><span>${esc(t('settings.sleepFrom'))}</span><input type="time" data-f="sleep-from" value="${esc(prefs.quiet_hours.starts_local)}"></label>
              <label class="field"><span>${esc(t('settings.sleepTo'))}</span><input type="time" data-f="sleep-to" value="${esc(prefs.quiet_hours.ends_local)}"></label>
            </div>
            <small class="help">${esc(t('settings.sleepHelp'))}</small>
            <button class="button" data-action="save-sleep">${esc(t('common.save'))}</button>
          </div>
        </div>
      </section>` : ''}

      ${healthSection(health)}

      ${updateSection(updates)}

      ${profile ? `<section class="section">
        <div class="section-head"><h2>${esc(t('settings.planning'))}</h2></div>
        <div class="card form">
          <div class="field"><span>${esc(t('settings.optionalEvents'))}</span>
            ${chipGroup('optional-policy', ['FAIL_CLOSED', 'OMIT_OPTIONAL', 'OMIT_OPTIONAL_AND_PREFERRED'].map((v) => [v, t(`settings.optional.${v}`)]), profile.optional_event_policy)}
            <small class="help" data-optional-help>${esc(t(`settings.optionalHelp.${profile.optional_event_policy}`))}</small></div>
        </div>
      </section>` : ''}

      ${aiSection(llm)}

      <section class="section">
        <div class="section-head"><h2>${esc(t('settings.appearance'))}</h2></div>
        <div class="card form">
          <div class="field"><span>${esc(t('settings.language'))}</span>${chipGroup('locale', LOCALES, getLocale())}</div>
          <div class="field"><span>${esc(t('settings.theme'))}</span>${chipGroup('theme', [['system', t('settings.theme.system')], ['light', t('settings.theme.light')], ['dark', t('settings.theme.dark')]], getTheme())}</div>
        </div>
      </section>

      <section class="section">
        <div class="section-head"><h2>${esc(t('settings.data'))}</h2></div>
        <div class="card">
          <p class="muted">${esc(t('settings.exportHelp'))}</p>
          <button class="button wide" data-action="account-export">${esc(t('settings.export'))}</button>
          <hr>
          <p class="muted">${esc(t('settings.deleteHelp', { n: deletion.tombstone_retention_days }))}</p>
          <button class="button danger ghost wide" data-action="account-delete-preview">${esc(t('settings.deleteReview'))}</button>
        </div>
      </section>

      ${syncSection(connectors)}

      <section class="section">
        <details class="card details" data-advanced>
          <summary>${icon('settings')} ${esc(t('settings.advanced'))}</summary>
          <p class="help">${esc(t('settings.advancedHelp'))}</p>
          ${isNative() ? `<div class="kv"><dt>${esc(t('settings.server'))}</dt><dd>${esc(session.server)}</dd></div>
            <button class="button ghost wide" data-action="settings-server">${icon('server')}${esc(t('settings.changeServer'))}</button>` : ''}
          <button class="button ghost wide" data-nav="evidence">${icon('evidence')}${esc(t('nav.evidence'))}</button>
          <dl class="kv-list">
            ${kv(t('settings.version'), diag.version)}
            ${kv(t('settings.schema'), `v${diag.schema_version}`)}
            ${kv(t('settings.revision'), diag.server_revision)}
            ${kv(t('settings.binding'), diag.account_binding)}
            ${diag.latest_plan ? kv(t('settings.latestPlan'), `${diag.latest_plan.feasibility_status} · ${fmtDateTime(diag.latest_plan.generated_at)}`) : ''}
            ${diag.latest_plan ? kv(t('settings.planRevision'), `${diag.latest_plan.plan_revision} · ${String(diag.latest_plan.input_hash || '').slice(0, 12)}`) : ''}
            ${kv(t('settings.series'), diag.recurring_template_count)}
            ${(diag.reminder_delivery_state || []).map((n) => kv(`Push ${n.state}`, n.count)).join('')}
            ${diag.reminder_worker ? kv('Reminder worker', diag.reminder_worker.state) : ''}
            ${Object.entries(diag.external_capabilities || {}).map(([name, state]) => kv(name.toUpperCase(), state)).join('')}
          </dl>
        </details>
      </section>`;
  },
  mount(root, _data, ctx) {
    root.querySelectorAll('[data-update-pref]').forEach((input) => input.addEventListener('change', async () => {
      await appUpdateService.setPreferences({ [input.dataset.updatePref]: input.checked });
      toast(t('settings.saved'));
    }));
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name === 'locale') { setLocale(e.detail.value); ctx.relabel(); ctx.rerender(); }
      if (e.detail.name === 'theme') setTheme(e.detail.value);
      if (e.detail.name === 'update-channel') {
        const save = async () => { await appUpdateService.setPreferences({ channel: e.detail.value }); toast(t('settings.saved')); ctx.refresh(); };
        if (e.detail.value === UpdateChannel.BETA) {
          confirmSheet({ title: t('updates.betaConfirmTitle'), body: `<p>${esc(t('updates.betaConfirmBody'))}</p>`, confirmLabel: t('common.continue') })
            .then((ok) => (ok ? save() : ctx.refresh())).catch((err) => toast(errorMessage(err), { error: true }));
        } else save().catch((err) => toast(errorMessage(err), { error: true }));
      }
      if (e.detail.name === 'optional-policy') {
        const value = e.detail.value;
        root.querySelector('[data-optional-help]').textContent = t(`settings.optionalHelp.${value}`);
        api('/api/v1/settings/planning-profile')
          .then((profile) => api('/api/v1/settings/planning-profile', { method: 'PATCH', body: { expected_version: profile.version, optional_event_policy: value } }))
          .then(() => { invalidate(); toast(t('settings.saved')); })
          .catch((err) => toast(errorMessage(err), { error: true }));
      }
      if (e.detail.name === 'intensity') {
        const value = e.detail.value;
        root.querySelector('[data-intensity-help]').textContent = t(`settings.remindHelp.${value}`);
        const body = value === 'OFF' ? { enabled: false } : { enabled: true, intensity: value };
        api('/api/v1/notification-preferences', { method: 'PATCH', body })
          .then(() => toast(t('settings.saved')))
          .catch((err) => toast(errorMessage(err), { error: true }));
      }
    });
  },
  actions: {
    logout: async () => {
      const ok = await confirmSheet({ title: t('settings.logoutTitle'), body: `<p>${esc(t('settings.logoutBody'))}</p>`, confirmLabel: t('settings.logout') });
      if (ok) await logout();
    },
    'settings-server': async () => {
      const ok = await confirmSheet({ title: t('settings.changeServer'), body: `<p>${esc(t('settings.changeServerBody'))}</p>`, confirmLabel: t('common.continue') });
      // The current server/session stays in use until the candidate passes its
      // health/protocol probe AND a sign-in there succeeds (see welcome.js).
      if (ok) shell.go('welcome', { step: 'server' });
    },
    'account-export': (el) => exportAccount(el),
    'save-sleep': (el, ctx) => saveSleep(el, ctx),
    'update-check': async (el, ctx) => { setBusy(el, true); try { await appUpdateService.checkForUpdates({ manual: true }); } catch (err) { toast(errorMessage(err), { error: true }); } finally { setBusy(el, false); ctx.refresh(); } },
    'update-download': async (el, ctx) => { setBusy(el, true); try { await appUpdateService.download(); } catch (err) { toast(errorMessage(err), { error: true }); } finally { setBusy(el, false); ctx.refresh(); } },
    'update-apply': async (el, ctx) => { setBusy(el, true); try { await appUpdateService.applyAndRestart(); } catch (err) { toast(errorMessage(err), { error: true }); } finally { setBusy(el, false); ctx.refresh(); } },
    'update-permission': async () => { await appUpdateService.adapter.openInstallPermission(); },
    'account-delete-preview': (_el, ctx) => deleteSheet(ctx.view._deletion),
    ...aiActions,
    ...healthActions,
    ...syncActions,
  },
};
