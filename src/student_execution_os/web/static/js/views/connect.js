import { api } from '../api.js';
import { t } from '../i18n.js';
import { esc, toast, errorMessage, setBusy, chipGroup, chipValue } from '../ui.js';
import { scopeChecklist, checkedScopes } from '../connected-apps.js';

// OAuth consent for an MCP client (ChatGPT, Codex, ...). The server validated the
// client and redirect before sending the person here; approving issues a capability
// grant limited to the ticked scopes, revocable in Settings → Connected apps.
export default {
  id: 'connect',
  tab: 'more',
  detail: true,
  title: () => t('apps.connectTitle'),
  async load({ params }) {
    return { data: await api(`/api/v1/oauth/requests/${encodeURIComponent(params[0] || '')}`) };
  },
  render(request) {
    return `<section class="section" data-connect="${esc(request.id)}">
      <div class="card form">
        <h2>${esc(t('apps.connectAsk', { client: request.client_name }))}</h2>
        <p class="muted">${esc(t('apps.connectWhere', { host: request.redirect_host }))}</p>
        ${scopeChecklist(request.scopes, new Set(request.requested_scopes))}
        <div class="field"><span>${esc(t('apps.lifetime'))}</span>
          ${chipGroup('connect-days', [['30', t('apps.days', { n: 30 })], ['90', t('apps.days', { n: 90 })], ['365', t('apps.days', { n: 365 })]], String(request.default_expires_in_days))}
        </div>
        <p class="help">${esc(t('apps.connectHelp'))}</p>
        <div class="button-row">
          <button class="button ghost" data-action="connect-deny">${esc(t('apps.deny'))}</button>
          <button class="button primary" data-action="connect-allow">${esc(t('apps.allow'))}</button>
        </div>
      </div>
    </section>`;
  },
  actions: {
    'connect-allow': async (el, ctx) => {
      const root = el.closest('[data-connect]');
      const scopes = checkedScopes(root);
      if (!scopes.length) { toast(t('apps.pickScope'), { error: true }); return; }
      setBusy(el, true);
      try {
        const answer = await api(`/api/v1/oauth/requests/${encodeURIComponent(root.dataset.connect)}/approve`, {
          method: 'POST', body: { scopes, expires_in_days: Number(chipValue(root, 'connect-days')) } });
        location.assign(answer.redirect);
      } catch (err) { setBusy(el, false); toast(errorMessage(err), { error: true }); ctx.refresh?.(); }
    },
    'connect-deny': async (el) => {
      const root = el.closest('[data-connect]');
      setBusy(el, true);
      try {
        const answer = await api(`/api/v1/oauth/requests/${encodeURIComponent(root.dataset.connect)}/deny`, { method: 'POST' });
        location.assign(answer.redirect);
      } catch (err) { setBusy(el, false); toast(errorMessage(err), { error: true }); }
    },
  },
};
