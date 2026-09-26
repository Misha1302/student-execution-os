import { api } from './api.js';
import { invalidate } from './store.js';
import { t, fmtDateTime } from './i18n.js';
import { esc, icon, chip, kv, openSheet, chipGroup, chipValue, toast, errorMessage, setBusy, confirmSheet } from './ui.js';

// Settings → AI: the account's own LLM key (BYOK). The key is sent once, to the
// server, and never read back: the server answers with a masked hint only. Nothing
// here is written to device storage.

const MODEL_HINT = { openai: 'gpt-5-mini', anthropic: 'claude-haiku-4-5', 'openai-compatible': 'llama-3.3-70b' };
const STATUS_TONE = { OK: 'ok', UNTESTED: 'muted', RATE_LIMITED: 'warn', UNREACHABLE: 'warn', PROVIDER_ERROR: 'warn' };
const STEPS = ['key', 'endpoint', 'model', 'format'];
// The last connection test of this session: which steps it proved (shown under the status).
let lastTest = null;

export async function loadAiSettings() {
  try { return await api('/api/v1/settings/llm'); } catch (err) { return { error: err }; }
}

function providerLabel(llm, id) {
  return (llm.providers || []).find((p) => p.id === id)?.label || id;
}

// A status this client has no words for (an app built before the server added it)
// is still described, never shown as a raw translation key.
function statusText(kind, status) {
  const key = `ai.${kind}.${status}`;
  return t(key) === key ? t(`ai.${kind}.other`, { status }) : t(key);
}

function statusChip(status) {
  return chip(statusText('status', status), STATUS_TONE[status] || 'danger');
}

export function aiSection(llm) {
  let body;
  if (!llm || llm.error) {
    body = `<p class="muted">${esc(llm?.error?.code === 'NETWORK' ? t('ai.offline') : errorMessage(llm?.error))}</p>`;
  } else if (llm.credential) {
    const c = llm.credential;
    body = `<dl class="kv-list">
        ${kv(t('ai.provider'), providerLabel(llm, c.provider))}
        ${kv(t('ai.model'), c.model)}
        ${c.base_url ? kv(t('ai.baseUrl'), c.base_url) : ''}
        ${kv(t('ai.key'), c.key_hint)}
      </dl>
      <div class="row static"><span class="row-main"><small>${esc(statusText('statusHelp', c.status))}</small>
        ${c.last_checked_at ? `<small>${esc(t('ai.checkedAt', { when: fmtDateTime(c.last_checked_at) }))}</small>` : ''}</span>${statusChip(c.status)}</div>
      ${lastTest && lastTest.status === c.status ? testSteps(lastTest) : ''}
      <div class="button-row">
        <button class="button ghost" data-action="ai-test">${esc(t('ai.test'))}</button>
        <button class="button ghost" data-action="ai-edit">${esc(t('ai.change'))}</button>
        <button class="button danger ghost" data-action="ai-delete">${esc(t('ai.remove'))}</button>
      </div>`;
  } else if (llm.source === 'PLATFORM_MANAGED') {
    body = `<p class="muted">${esc(t('ai.managed'))}</p>
      ${llm.storage_available ? `<button class="button ghost wide" data-action="ai-edit">${esc(t('ai.ownKey'))}</button>` : ''}`;
  } else {
    body = `<p class="muted">${esc(t('ai.none'))}</p>
      ${llm.storage_available
        ? `<button class="button wide" data-action="ai-edit">${icon('spark')}${esc(t('ai.connect'))}</button>`
        : `<p class="help">${esc(t('ai.unavailable'))}</p>`}`;
  }
  return `<section class="section" data-ai>
      <div class="section-head"><h2>${esc(t('ai.title'))}</h2></div>
      <div class="card">${body}</div>
    </section>`;
}

// "Ключ ✓ · Адрес ✓ · Модель ✓ · Формат ответа ✗": what the smoke test proved.
function testSteps(result) {
  const passed = new Set(result.checked || []);
  const failedAt = result.ok ? null : STEPS.find((step) => !passed.has(step));
  return `<ul class="check-steps">${STEPS.map((step) => {
    const state = passed.has(step) ? 'ok' : step === failedAt ? 'fail' : 'skip';
    return `<li class="step-${state}">${icon(state === 'ok' ? 'check' : state === 'fail' ? 'x' : 'question')}<span>${esc(t(`ai.step.${step}`))}</span></li>`;
  }).join('')}</ul>${result.latency_ms != null && result.ok ? `<p class="help">${esc(t('ai.latency', { ms: result.latency_ms }))}</p>` : ''}`;
}

function editSheet(llm, onDone) {
  const current = llm.credential;
  let provider = current?.provider || 'openai';
  const dialog = openSheet({
    title: current ? t('ai.change') : t('ai.connect'),
    body: `<p class="help">${esc(t('ai.byokHelp'))}</p>
      <div class="field"><span>${esc(t('ai.provider'))}</span>${chipGroup('ai-provider', llm.providers.map((p) => [p.id, p.label]), provider)}</div>
      <label class="field"><span>${esc(t('ai.model'))}</span>
        <input id="ai-model" autocomplete="off" autocapitalize="off" spellcheck="false" value="${esc(current?.model || '')}"></label>
      <label class="field" data-base-url><span>${esc(t('ai.baseUrl'))}</span>
        <input id="ai-base-url" type="url" inputmode="url" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="https://…/v1" value="${esc(current?.base_url || '')}">
        <small class="help">${esc(t('ai.baseUrlHelp'))}</small></label>
      <label class="field"><span>${esc(t('ai.key'))}</span>
        <input id="ai-key" type="password" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
      <p class="help" data-key-help></p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  const keyInput = dialog.querySelector('#ai-key');
  const baseInput = dialog.querySelector('#ai-base-url');
  const sync = () => {
    const needsBase = llm.providers.find((p) => p.id === provider)?.requires_base_url;
    dialog.querySelector('[data-base-url]').hidden = !needsBase;
    dialog.querySelector('#ai-model').placeholder = MODEL_HINT[provider] || '';
    // A saved key can be kept only while it keeps going to the same provider/address.
    const keep = current && current.provider === provider && (!needsBase || baseInput.value.trim().replace(/\/+$/, '') === (current.base_url || ''));
    keyInput.placeholder = keep ? t('ai.keepKey', { hint: current.key_hint }) : 'sk-…';
    dialog.querySelector('[data-key-help]').textContent = keep ? t('ai.keepKeyHelp') : t('ai.keyHelp');
    return keep;
  };
  dialog.addEventListener('chipchange', (e) => { if (e.detail.name === 'ai-provider') { provider = e.detail.value; sync(); } });
  baseInput.addEventListener('input', sync);
  sync();
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const button = e.currentTarget;
    const body = { provider: chipValue(dialog, 'ai-provider') || provider, model: dialog.querySelector('#ai-model').value.trim() };
    if (!dialog.querySelector('[data-base-url]').hidden) body.base_url = baseInput.value.trim();
    const key = keyInput.value.trim();
    if (key) body.api_key = key;
    else if (!sync()) { toast(t('ai.keyRequired'), { error: true }); keyInput.focus(); return; }
    if (!body.model) { toast(t('ai.modelRequired'), { error: true }); dialog.querySelector('#ai-model').focus(); return; }
    if (current && !key) body.expected_version = current.version;
    setBusy(button, true);
    try {
      await api('/api/v1/settings/llm', { method: 'PUT', body });
      keyInput.value = '';
      dialog.close('saved');
      invalidate();
      await runTest();
      onDone();
    } catch (err) {
      toast(errorMessage(err), { error: true });
      setBusy(button, false);
    }
  });
}

async function runTest() {
  try {
    const result = await api('/api/v1/settings/llm/test', { method: 'POST', timeoutMs: 60000 });
    lastTest = result;
    toast(result.ok ? t('ai.testOk') : statusText('statusHelp', result.status), { error: !result.ok });
  } catch (err) {
    toast(errorMessage(err), { error: true });
  }
}

export const aiActions = {
  'ai-edit': async (_el, ctx) => {
    const llm = ctx.view._llm;
    if (llm && !llm.error) editSheet(llm, () => ctx.refresh());
  },
  'ai-test': async (el, ctx) => {
    setBusy(el, true);
    await runTest();
    setBusy(el, false);
    ctx.refresh();
  },
  'ai-delete': async (_el, ctx) => {
    const ok = await confirmSheet({ title: t('ai.removeTitle'), body: `<p>${esc(t('ai.removeBody'))}</p>`, confirmLabel: t('ai.remove'), danger: true });
    if (!ok) return;
    try {
      await api('/api/v1/settings/llm', { method: 'DELETE' });
      invalidate();
      toast(t('ai.removed'));
      ctx.refresh();
    } catch (err) {
      toast(errorMessage(err), { error: true });
    }
  },
};
