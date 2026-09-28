import { api, session } from './api.js';
import { t, fmtDateTime } from './i18n.js';
import { esc, chip, toast, errorMessage, setBusy, confirmSheet, openSheet, chipGroup, chipValue } from './ui.js';

// Settings → Connected apps: least-authority grants for ChatGPT, Codex and other MCP
// clients. The token is shown exactly once, right after creation; afterwards only its
// label, scopes and dates are visible. Nothing here is written to device storage.

export async function loadConnectedApps() {
  try { return await api('/api/v1/settings/capabilities'); } catch (error) { return { error }; }
}

export function mcpUrl() {
  return `${(session.server || location.origin).replace(/\/$/, '')}/mcp`;
}

function scopeLabel(scope) {
  const key = `apps.scope.${scope}`;
  return t(key) === key ? scope : t(key);
}

function grantRow(grant) {
  const expired = grant.expires_at && new Date(grant.expires_at) <= new Date();
  const state = grant.revoked_at ? chip(t('apps.revoked'), 'muted') : expired ? chip(t('apps.expired'), 'warn') : chip(t('apps.active'), 'ok');
  return `<div class="row static" data-grant="${esc(grant.id)}">
      <span class="row-main"><strong>${esc(grant.label)}</strong>
        <small>${esc(grant.scopes.map(scopeLabel).join(' · '))}</small>
        <small>${esc(t('apps.used', { when: grant.last_used_at ? fmtDateTime(grant.last_used_at) : t('apps.never') }))}${grant.expires_at ? ` · ${esc(t('apps.expires', { when: fmtDateTime(grant.expires_at) }))}` : ''}</small>
      </span>
      ${state}
      ${grant.revoked_at ? '' : `<button class="button danger ghost" data-action="app-revoke" data-id="${esc(grant.id)}" data-label="${esc(grant.label)}">${esc(t('apps.revoke'))}</button>`}
    </div>`;
}

export function connectedAppsSection(state) {
  const head = `<div class="section-head"><h2>${esc(t('apps.title'))}</h2></div>`;
  if (!state || state.error) {
    return `<section class="section" data-connected-apps>${head}<div class="card"><p class="muted">${esc(errorMessage(state?.error))}</p></div></section>`;
  }
  const grants = state.grants || [];
  return `<section class="section" data-connected-apps>${head}
    <div class="card form">
      <p class="muted">${esc(t('apps.help'))}</p>
      <p class="help">${esc(t('apps.mcpUrl'))} <code data-mcp-url>${esc(mcpUrl())}</code></p>
      ${grants.length ? grants.map(grantRow).join('') : `<p class="muted">${esc(t('apps.none'))}</p>`}
      <button class="button wide" data-action="app-new">${esc(t('apps.new'))}</button>
    </div>
  </section>`;
}

export function scopeChecklist(scopes, checked) {
  return `<div class="field" data-scopes>${scopes.map(({ id, description }) => `
      <label class="setting-toggle"><span><strong>${esc(scopeLabel(id))}</strong><small>${esc(description)}</small></span>
        <input type="checkbox" data-scope="${esc(id)}" ${checked.has(id) ? 'checked' : ''}></label>`).join('')}</div>`;
}

export function checkedScopes(root) {
  return [...root.querySelectorAll('[data-scope]:checked')].map((input) => input.dataset.scope);
}

function showToken(token) {
  const dialog = openSheet({
    title: t('apps.tokenTitle'),
    body: `<div class="form">
      <p class="text-danger">${esc(t('apps.tokenOnce'))}</p>
      <label class="field"><span>${esc(t('apps.token'))}</span><input readonly data-token value="${esc(token)}"></label>
      <label class="field"><span>${esc(t('apps.mcpUrl'))}</span><input readonly value="${esc(mcpUrl())}"></label>
      <p class="help">${esc(t('apps.codexHelp'))}</p>
      <pre class="code" data-codex>[mcp_servers.botay]\nurl = "${esc(mcpUrl())}"\nbearer_token_env_var = "BOTAY_TOKEN"</pre>
    </div>`,
    actions: `<button type="button" class="button" data-copy>${esc(t('apps.copy'))}</button>
      <button value="done" class="button primary">${esc(t('common.close'))}</button>`,
  });
  dialog.querySelector('[data-copy]').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(token); toast(t('apps.copied')); }
    catch { dialog.querySelector('[data-token]').select(); }
  });
}

function newGrantSheet(state, ctx) {
  const dialog = openSheet({
    title: t('apps.new'),
    full: true,
    body: `<div class="form">
      <label class="field"><span>${esc(t('apps.label'))}</span><input data-label maxlength="80" value="ChatGPT"></label>
      ${scopeChecklist(state.scopes || [], new Set(['today:read', 'tasks:read', 'calendar:read']))}
      <div class="field"><span>${esc(t('apps.lifetime'))}</span>
        ${chipGroup('app-days', [['30', t('apps.days', { n: 30 })], ['90', t('apps.days', { n: 90 })], ['365', t('apps.days', { n: 365 })]], '90')}
      </div>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-create>${esc(t('apps.create'))}</button>`,
  });
  dialog.querySelector('[data-create]').addEventListener('click', async (e) => {
    const scopes = checkedScopes(dialog);
    if (!scopes.length) { toast(t('apps.pickScope'), { error: true }); return; }
    setBusy(e.currentTarget, true);
    try {
      const created = await api('/api/v1/settings/capabilities', { method: 'POST', body: {
        label: dialog.querySelector('[data-label]').value.trim(), scopes,
        expires_in_days: Number(chipValue(dialog, 'app-days')),
      } });
      dialog.close('saved');
      showToken(created.token);
      ctx.refresh();
    } catch (err) {
      toast(errorMessage(err), { error: true });
    } finally {
      setBusy(e.currentTarget, false);
    }
  });
}

export const connectedAppsActions = {
  'app-new': (_el, ctx) => newGrantSheet(ctx.data.apps || {}, ctx),
  'app-revoke': async (el, ctx) => {
    const ok = await confirmSheet({ title: t('apps.revokeTitle', { label: el.dataset.label }), body: `<p>${esc(t('apps.revokeBody'))}</p>`, confirmLabel: t('apps.revoke') });
    if (!ok) return;
    try {
      await api(`/api/v1/settings/capabilities/${encodeURIComponent(el.dataset.id)}`, { method: 'DELETE' });
      toast(t('apps.revokedToast'));
      ctx.refresh();
    } catch (err) { toast(errorMessage(err), { error: true }); }
  },
};
