import { load } from '../store.js';
import { t, code, fmtDuration } from '../i18n.js';
import { esc, icon, chip, empty, setBusy } from '../ui.js';
import { change } from '../actions.js';

function metric(label, value, hint = '') {
  return `<article class="card">
    <span class="eyebrow">${esc(label)}</span>
    <strong class="big-time">${esc(value)}</strong>
    ${hint ? `<p class="muted">${esc(hint)}</p>` : ''}
  </article>`;
}

function calibrationCard(item) {
  const ratio = Number(item.median_ratio || 1);
  const percent = Number(item.median_percent_difference || 0);
  const pref = item.preference;
  const accepted = pref?.enabled && Number(pref.safety_multiplier || 1) > 1;
  const suggestion = item.suggested_multiplier;
  const copy = percent > 0
    ? t('reflection.underBy', { n: Math.abs(percent) })
    : percent < 0 ? t('reflection.overBy', { n: Math.abs(percent) }) : t('reflection.onEstimate');
  return `<article class="card" data-category="${esc(item.category)}">
    <div class="section-head"><div>
      <h3>${esc(code('category', item.category))}</h3>
      <p>${esc(t('reflection.sample', { n: item.sample_size }))} · ${esc(copy)}</p>
    </div>
      ${accepted ? chip(t('reflection.activeMultiplier', { n: Number(pref.safety_multiplier).toFixed(2) }), 'accent') : ''}
    </div>
    <dl class="mini-kv">
      <div><dt>${esc(t('reflection.medianRatio'))}</dt><dd>×${esc(ratio.toFixed(2))}</dd></div>
      <div><dt>${esc(t('reflection.meanRatio'))}</dt><dd>×${esc(Number(item.mean_ratio || 1).toFixed(2))}</dd></div>
    </dl>
    ${suggestion ? `<div class="banner info"><div><strong>${esc(t('reflection.suggestionTitle'))}</strong>
      <p>${esc(t('reflection.suggestionBody', { n: Number(suggestion).toFixed(2) }))}</p></div></div>
      <div class="now-actions">
        <button class="button primary" data-action="calibration-accept" data-category="${esc(item.category)}"
          data-multiplier="${Number(suggestion)}" data-version="${pref?.version || ''}">${esc(t('reflection.useSuggestion'))}</button>
        <button class="button ghost" data-action="calibration-hide" data-category="${esc(item.category)}"
          data-version="${pref?.version || ''}">${esc(t('reflection.dontSuggest'))}</button>
      </div>` : accepted ? `<div class="now-actions">
        <button class="button ghost" data-action="calibration-disable" data-category="${esc(item.category)}"
          data-version="${pref?.version || ''}">${esc(t('reflection.disableCalibration'))}</button>
      </div>` : ''}
  </article>`;
}

export default {
  id: 'reflection',
  tab: 'more',
  title: () => t('nav.reflection'),
  async load({ fresh }) {
    return load('/api/v1/reflection?days=7', { fresh });
  },
  render(data) {
    const most = data.most_underestimated;
    return `<section class="section">
      <div class="section-head"><div><h2>${esc(t('reflection.weekTitle'))}</h2>
        <p>${esc(t('reflection.help'))}</p></div></div>
      <div class="grid cards">
        ${metric(t('reflection.planned'), fmtDuration(data.planned_work_minutes || 0))}
        ${metric(t('reflection.actual'), fmtDuration(data.actual_work_minutes || 0),
          t('reflection.variance', { d: fmtDuration(Math.abs(data.variance_minutes || 0)), sign: Number(data.variance_minutes || 0) >= 0 ? '+' : '−' }))}
        ${metric(t('reflection.completed'), String(data.completed_tasks || 0))}
        ${metric(t('reflection.carryOver'), String(data.carry_over_count || 0))}
        ${metric(t('reflection.churn'), String(data.schedule_churn || 0), t('reflection.churnHint'))}
      </div>
      ${most ? `<div class="banner warn">${icon('clock')}<div><strong>${esc(t('reflection.patternTitle'))}</strong>
        <p>${esc(t('reflection.patternBody', { category: code('category', most.category), n: Math.abs(most.median_percent_difference || 0) }))}</p>
      </div></div>` : ''}
    </section>

    <section class="section">
      <div class="section-head"><div><h2>${esc(t('reflection.calibrationTitle'))}</h2>
        <p>${esc(t('reflection.calibrationHelp', { days: data.calibration_window_days || 90 }))}</p></div></div>
      ${data.calibration?.length ? `<div class="stack">${data.calibration.map(calibrationCard).join('')}</div>`
        : empty(t('reflection.noCalibration'), t('reflection.noCalibrationHelp'), 'clock')}
    </section>`;
  },
  actions: {
    async 'calibration-accept'(el) {
      setBusy(el, true);
      const payload = {
        category: el.dataset.category,
        safety_multiplier: Number(el.dataset.multiplier),
        enabled: true,
        suppress_suggestion: false,
      };
      if (el.dataset.version) payload.expected_version = Number(el.dataset.version);
      const result = await change('calibration.set', `calibration-${el.dataset.category}`, payload,
        { success: t('reflection.calibrationEnabled') });
      if (!result) setBusy(el, false);
    },
    async 'calibration-disable'(el) {
      setBusy(el, true);
      const payload = {
        category: el.dataset.category,
        safety_multiplier: 1,
        enabled: false,
        suppress_suggestion: false,
      };
      if (el.dataset.version) payload.expected_version = Number(el.dataset.version);
      const result = await change('calibration.set', `calibration-${el.dataset.category}`, payload,
        { success: t('reflection.calibrationDisabled') });
      if (!result) setBusy(el, false);
    },
    async 'calibration-hide'(el) {
      setBusy(el, true);
      const payload = {
        category: el.dataset.category,
        safety_multiplier: 1,
        enabled: false,
        suppress_suggestion: true,
      };
      if (el.dataset.version) payload.expected_version = Number(el.dataset.version);
      const result = await change('calibration.set', `calibration-${el.dataset.category}`, payload,
        { success: t('reflection.suggestionHidden') });
      if (!result) setBusy(el, false);
    },
  },
};
