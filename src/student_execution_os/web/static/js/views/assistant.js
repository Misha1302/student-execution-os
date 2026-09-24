import { load } from '../store.js';
import { api } from '../api.js';
import { t, code } from '../i18n.js';
import { esc, icon, kv, empty, openSheet, toast, errorMessage, setBusy } from '../ui.js';
import { mutate } from '../actions.js';
import { speechToText } from '../native.js';

async function interpretText(text) {
  const preview = await api('/api/v1/assistant/interpret', { method: 'POST', body: { text } });
  const actions = preview.actions || [];
  const dialog = openSheet({
    eyebrow: preview.provider,
    title: t('ask.previewActions'),
    body: `<div class="list">${actions.map((action) => `<article class="row"><span class="row-main"><strong>${esc(code('command', action.command))}</strong><small>${esc(JSON.stringify(action.payload))}</small>
      ${action.unresolved_fields.length ? `<small>${esc(t('ask.unresolved', { fields: action.unresolved_fields.join(', ') }))}</small>` : ''}</span></article>`).join('')}</div>
      <p class="help">${esc(t('ask.previewNoMutation'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-apply ${actions.some((action) => action.unresolved_fields.length) ? 'disabled' : ''}>${esc(t('ask.applyActions'))}</button>`,
  });
  dialog.querySelector('[data-apply]')?.addEventListener('click', async (event) => {
    setBusy(event.currentTarget, true);
    await mutate(() => api('/api/v1/assistant/apply', { method: 'POST', body: {
      batch_id: preview.batch_id, action_ids: actions.map((action) => action.id),
      confirmed_action_ids: actions.filter((action) => action.requires_confirmation).map((action) => action.id),
      idempotency_key: `assistant-${preview.batch_id}`,
    } }), { success: t('ask.executed') });
    dialog.close('applied');
  });
}

async function previewCancel(target) {
  let preview;
  try {
    preview = await api('/api/v1/agent/cancel/preview', {
      method: 'POST', body: { obligation_id: target.id, expected_version: target.version },
    });
  } catch (err) {
    toast(errorMessage(err), { error: true });
    return;
  }
  const dialog = openSheet({
    eyebrow: t('ask.intentEyebrow'),
    title: t('ask.confirmTitle'),
    body: `<div class="banner danger">${icon('alert')}<div><strong>${esc(code('command', preview.command))}</strong>
        <p>${esc(t('ask.target', { title: preview.target_title }))}<br>${esc(t('ask.scope', { scope: preview.scope }))}</p></div></div>
      <dl class="kv-list">
        ${kv(t('ask.expectedVersion'), preview.expected_version)}
        ${kv(t('ask.effect'), t('ask.effectText'))}
      </dl>
      <p class="help">${esc(t('ask.notAuthorization'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.keep'))}</button>
      <button type="button" class="button danger" data-confirm>${esc(t('ask.confirm'))}</button>`,
  });
  dialog.querySelector('[data-confirm]').addEventListener('click', async (e) => {
    setBusy(e.currentTarget, true);
    await mutate(() => api('/api/v1/agent/cancel/confirm-execute', {
      method: 'POST', body: { intent_id: preview.intent_id, idempotency_key: `app-${preview.intent_id}` },
    }), { success: t('ask.executed') });
    dialog.close('done');
  });
}

export default {
  id: 'assistant',
  tab: 'more',
  detail: true,
  title: () => t('nav.assistant'),
  async load({ fresh }) {
    const [caps, tasks, events] = await Promise.all([
      load('/api/v1/ask/capabilities', { fresh }),
      load('/api/v1/tasks', { fresh }),
      load('/api/v1/events', { fresh }),
    ]);
    const active = [
      ...tasks.data.map((x) => ({ ...x, kind: 'TASK' })),
      ...events.data.map((x) => ({ ...x, kind: 'EVENT' })),
    ].filter((x) => x.status === 'ACTIVE');
    return { data: { caps: caps.data, active }, stale: caps.stale || tasks.stale, fetchedAt: caps.fetchedAt };
  },
  render({ caps, active }) {
    this._active = active;
    return `
      <section class="hero-status status-unknown">
        <div class="hero-icon status-unknown">${icon('spark')}</div>
        <div class="hero-copy"><p class="eyebrow">${esc(t('ask.eyebrow'))}</p>
          <h2>${esc(t('ask.title'))}</h2>
          <p>${esc(caps.live_llm_provider ? t('ask.live') : t('ask.noProvider'))}</p></div>
      </section>
      <section class="section">
        <div class="card form">
          <label class="field"><span>${esc(t('ask.naturalInput'))}</span><textarea id="assistant-input" rows="3" placeholder="${esc(t('ask.naturalPlaceholder'))}"></textarea></label>
          <div class="button-row"><button class="button primary" data-action="assistant-interpret">${esc(t('ask.interpret'))}</button>
          <button class="button ghost" data-action="assistant-voice">${esc(t('ask.voice'))}</button></div>
        </div>
      </section>
      <section class="section">
        <div class="section-head"><h2>${esc(t('ask.explainTitle'))}</h2></div>
        <div class="list">
          <button class="row" data-nav="plan"><span class="row-icon tone-accent">${icon('question')}</span><span class="row-main"><strong>${esc(t('ask.q1'))}</strong><small>${esc(t('ask.a1'))}</small></span>${icon('chevron')}</button>
          <button class="row" data-nav="evidence"><span class="row-icon tone-accent">${icon('evidence')}</span><span class="row-main"><strong>${esc(t('ask.q2'))}</strong><small>${esc(t('ask.a2'))}</small></span>${icon('chevron')}</button>
          <button class="row" data-nav="today"><span class="row-icon tone-accent">${icon('clock')}</span><span class="row-main"><strong>${esc(t('ask.q3'))}</strong><small>${esc(t('ask.a3'))}</small></span>${icon('chevron')}</button>
        </div>
      </section>
      <section class="section">
        <div class="section-head"><h2>${esc(t('ask.actionTitle'))}</h2></div>
        <p class="help pad">${esc(t('ask.actionHelp'))}</p>
        ${active.length ? `<div class="card form">
          <label class="field"><span>${esc(t('ask.pick'))}</span>
            <select id="agent-target">${active.map((o, i) => `<option value="${i}">${esc(o.title)} · ${esc(code('kind', o.kind))}</option>`).join('')}</select></label>
          <button class="button danger wide" data-action="agent-preview">${esc(t('ask.preview'))}</button>
        </div>` : empty(t('ask.noActive'), '', 'check')}
      </section>`;
  },
  actions: {
    async 'assistant-interpret'() {
      const text = document.getElementById('assistant-input')?.value.trim();
      if (text) await interpretText(text);
    },
    async 'assistant-voice'() {
      try {
        const text = await speechToText();
        const input = document.getElementById('assistant-input');
        if (input) input.value = text;
        if (text) await interpretText(text);
      } catch (error) { toast(error.message, { error: true }); }
    },
    'agent-preview'(_el, ctx) {
      const index = Number(document.getElementById('agent-target')?.value);
      const target = ctx.view._active?.[index];
      if (target) previewCancel(target);
    },
  },
};
