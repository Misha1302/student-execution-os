import { load, clearAll } from '../store.js';
import { api, session, clearAuth } from '../api.js';
import { t, code, fmtDateTime, getLocale, setLocale, LOCALES } from '../i18n.js';
import { esc, icon, chip, kv, openSheet, chipGroup, toast, errorMessage, setBusy, confirmSheet } from '../ui.js';
import { isNative, saveJson, prefGet, prefSet } from '../native.js';
import { getTheme, setTheme } from '../theme.js';
import { shell } from '../actions.js';

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
    eyebrow: t('settings.deleteEyebrow', { policy: policy.policy_version }),
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

export default {
  id: 'settings',
  tab: 'more',
  detail: true,
  title: () => t('nav.settings'),
  async load({ fresh }) {
    const [diag, deletion] = await Promise.all([
      load('/api/v1/settings/diagnostics', { fresh }),
      load('/api/v1/account/deletion-policy', { fresh }),
    ]);
    return { data: { diag: diag.data, deletion: deletion.data }, stale: diag.stale, fetchedAt: diag.fetchedAt };
  },
  render({ diag, deletion }) {
    this._deletion = deletion;
    const sessionMode = session.authMode === 'session';
    const connectors = (diag.connector_health || []).map((c) => `<div class="row static">
      <span class="row-main"><strong>${esc(c.provider)}</strong><small>${esc(t('ev.lastSync'))}: ${esc(fmtDateTime(c.last_successful_complete_sync_at))}</small></span>
      ${chip(code('health', c.health_status), c.health_status === 'CURRENT' ? 'ok' : 'warn')}
    </div>`).join('');
    return `
      <section class="section">
        <div class="section-head"><h2>${esc(t('settings.account'))}</h2></div>
        <div class="card">
          <div class="account-row">
            <span class="avatar">${esc((session.user?.login || 'U').slice(0, 1).toUpperCase())}</span>
            <div class="row-main"><strong>${esc(sessionMode ? session.user?.login : t('settings.localAccount'))}</strong>
              <small>${esc(isNative() ? session.server : location.host)}</small></div>
          </div>
          ${sessionMode || isNative() ? `<div class="button-row">
            ${isNative() ? `<button class="button ghost" data-action="settings-server">${icon('server')}${esc(t('settings.changeServer'))}</button>` : ''}
            ${sessionMode ? `<button class="button ghost" data-action="logout">${icon('logout')}${esc(t('settings.logout'))}</button>` : ''}
          </div>` : ''}
        </div>
      </section>

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

      ${connectors ? `<section class="section"><div class="section-head"><h2>${esc(t('settings.connections'))}</h2></div><div class="list">${connectors}</div></section>` : ''}

      <section class="section">
        <details class="card details">
          <summary>${icon('settings')} ${esc(t('settings.diagnostics'))}</summary>
          <dl class="kv-list">
            ${kv(t('settings.version'), diag.version)}
            ${kv(t('settings.schema'), `v${diag.schema_version}`)}
            ${kv(t('settings.revision'), diag.server_revision)}
            ${kv(t('settings.binding'), diag.account_binding)}
            ${diag.latest_plan ? kv(t('settings.latestPlan'), `${diag.latest_plan.feasibility_status} · ${fmtDateTime(diag.latest_plan.generated_at)}`) : ''}
            ${kv(t('settings.series'), diag.recurring_template_count)}
            ${(diag.reminder_delivery_state || []).map((n) => kv(n.state, n.count)).join('')}
            ${diag.reminder_worker ? kv('Reminder worker', diag.reminder_worker.state) : ''}
            ${Object.entries(diag.external_capabilities || {}).map(([name, state]) => kv(name.toUpperCase(), state)).join('')}
          </dl>
        </details>
      </section>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('chipchange', (e) => {
      if (e.detail.name === 'locale') { setLocale(e.detail.value); ctx.relabel(); ctx.rerender(); }
      if (e.detail.name === 'theme') setTheme(e.detail.value);
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
    'account-delete-preview': (_el, ctx) => deleteSheet(ctx.view._deletion),
  },
};
