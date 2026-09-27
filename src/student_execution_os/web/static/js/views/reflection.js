import { load } from '../store.js';
import { t, fmtDuration } from '../i18n.js';
import { esc, icon, chip, setBusy, toast } from '../ui.js';
import { change } from '../actions.js';

const text = (value) => String(value || '');

function metric(label, value) {
  return `<div class="card metric-card"><small class="muted">${esc(label)}</small><strong>${esc(value)}</strong></div>`;
}

function notes(prefix, value = {}) {
  return `<div class="form compact-form" data-note-form="${esc(prefix)}">
    <label class="field"><span>${esc(t('reflection.summary'))}</span>
      <textarea rows="2" data-note="summary" maxlength="4000">${esc(text(value.summary))}</textarea></label>
    <label class="field"><span>${esc(t('reflection.wins'))}</span>
      <textarea rows="2" data-note="wins" maxlength="4000">${esc(text(value.wins))}</textarea></label>
    <label class="field"><span>${esc(t('reflection.blockers'))}</span>
      <textarea rows="2" data-note="blockers" maxlength="4000">${esc(text(value.blockers))}</textarea></label>
    <label class="field"><span>${esc(t('reflection.adjustment'))}</span>
      <textarea rows="2" data-note="adjustment" maxlength="4000">${esc(text(value.adjustment))}</textarea></label>
  </div>`;
}

function readNotes(root, name) {
  const form = root.querySelector(`[data-note-form="${name}"]`);
  const get = (field) => form?.querySelector(`[data-note="${field}"]`)?.value?.trim() || null;
  return { summary: get('summary'), wins: get('wins'), blockers: get('blockers'), adjustment: get('adjustment') };
}

function intentSection(data) {
  const intent = data.intent || {};
  const selected = new Set(intent.task_ids || []);
  const candidates = data.focus_candidates || [];
  return `<section class="card">
    <div class="section-head"><div><h3>${esc(t('reflection.intentTitle'))}</h3>
      <p>${esc(t('reflection.intentHelp'))}</p></div>
      ${intent._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
    </div>
    <label class="field"><span>${esc(t('reflection.focusNote'))}</span>
      <textarea rows="2" data-intent-note maxlength="2000">${esc(text(intent.focus_note))}</textarea></label>
    <div class="field"><span>${esc(t('reflection.focusTasks'))}</span>
      <div class="check-list">
        ${candidates.length ? candidates.map((task) => `<label class="check-row">
          <input type="checkbox" data-focus-task value="${esc(task.id)}" ${selected.has(task.id) ? 'checked' : ''}>
          <span><strong>${esc(task.title)}</strong><small>${task.remaining_effort_minutes == null ? esc(t('card.effort.unknown')) : esc(t('tasks.left', { d: fmtDuration(task.remaining_effort_minutes) }))}</small></span>
        </label>`).join('') : `<p class="muted">${esc(t('reflection.noFocusTasks'))}</p>`}
      </div>
      <small class="help">${esc(t('reflection.focusLimit'))}</small>
    </div>
    <button class="button primary" data-action="reflection-save-intent">${esc(t('common.save'))}</button>
  </section>`;
}

function statsSection(data) {
  const day = data.day_stats || {};
  const week = data.week_stats || {};
  const delta = Number(day.delta_minutes || 0);
  return `<section class="section">
    <div class="section-head"><div><h2>${esc(t('reflection.realityTitle'))}</h2>
      <p>${esc(t('reflection.realityHelp'))}</p></div></div>
    <div class="metric-grid">
      ${metric(t('reflection.plannedToday'), fmtDuration(day.planned_work_minutes || 0))}
      ${metric(t('reflection.actualToday'), fmtDuration(day.actual_work_minutes || 0))}
      ${metric(t('reflection.deltaToday'), `${delta > 0 ? '+' : delta < 0 ? '−' : ''}${fmtDuration(Math.abs(delta))}`)}
      ${metric(t('reflection.completedToday'), String(day.completed_obligations || 0))}
    </div>
    <p class="help">${esc(t('reflection.planBasis.' + (day.plan_basis || 'NO_PLAN_SNAPSHOT')))}</p>
    <div class="metric-grid">
      ${metric(t('reflection.plannedWeek'), fmtDuration(week.planned_work_minutes || 0))}
      ${metric(t('reflection.actualWeek'), fmtDuration(week.actual_work_minutes || 0))}
      ${metric(t('reflection.completedWeek'), String(week.completed_obligations || 0))}
    </div>
  </section>`;
}

function calibrationSection(data) {
  const calibration = data.calibration || {};
  const accepted = calibration.accepted;
  const ratio = calibration.median_actual_to_planned_ratio;
  const suggested = calibration.suggested_multiplier;
  return `<section class="card">
    <div class="section-head"><div><h3>${esc(t('reflection.calibrationTitle'))}</h3>
      <p>${esc(t('reflection.calibrationHelp'))}</p></div>
      ${accepted ? chip(t('reflection.savedMultiplier', { x: Number(accepted.multiplier).toFixed(2) }), accepted._pending ? 'warn' : 'accent') : ''}
    </div>
    <dl class="kv-list">
      <div class="kv"><dt>${esc(t('reflection.samples'))}</dt><dd>${esc(String(calibration.sample_count || 0))}</dd></div>
      <div class="kv"><dt>${esc(t('reflection.medianRatio'))}</dt><dd>${ratio == null ? '—' : esc('×' + Number(ratio).toFixed(2))}</dd></div>
      <div class="kv"><dt>${esc(t('reflection.bias'))}</dt><dd>${esc(t('reflection.bias.' + (calibration.bias || 'NO_DATA')))}</dd></div>
    </dl>
    <p class="help">${esc(t('reflection.noAutoMutation'))}</p>
    ${suggested != null ? `<button class="button primary" data-action="reflection-accept-calibration"
      data-multiplier="${Number(suggested)}">${esc(t('reflection.acceptMultiplier', { x: Number(suggested).toFixed(2) }))}</button>` : ''}
  </section>`;
}

export default {
  id: 'reflection',
  tab: 'more',
  title: () => t('nav.reflection'),
  async load({ fresh }) {
    return load('/api/v1/reflection', { fresh });
  },
  render(data) {
    this._data = data;
    const daily = data.daily_reflection || {};
    const weekly = data.weekly_review || {};
    return `<div class="stack" data-reflection-root>
      ${intentSection(data)}
      ${statsSection(data)}
      <section class="card">
        <div class="section-head"><div><h3>${esc(t('reflection.dailyTitle'))}</h3>
          <p>${esc(t('reflection.dailyHelp'))}</p></div>
          ${daily._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
        </div>
        ${notes('daily', daily)}
        <button class="button primary" data-action="reflection-save-daily">${esc(t('common.save'))}</button>
      </section>
      <section class="card">
        <div class="section-head"><div><h3>${esc(t('reflection.weeklyTitle'))}</h3>
          <p>${esc(t('reflection.weeklyHelp'))}</p></div>
          ${weekly._pending ? chip(t('sync.pendingShort'), 'warn') : ''}
        </div>
        ${notes('weekly', weekly)}
        <button class="button primary" data-action="reflection-save-weekly">${esc(t('common.save'))}</button>
      </section>
      ${calibrationSection(data)}
    </div>`;
  },
  actions: {
    async 'reflection-save-intent'(el, ctx) {
      const root = el.closest('[data-reflection-root]');
      const ids = [...root.querySelectorAll('[data-focus-task]:checked')].map((x) => x.value);
      if (ids.length > 5) { toast(t('reflection.focusTooMany'), { error: true }); return; }
      setBusy(el, true);
      await change('intent.upsert', `intent-${ctx.data.local_date}`, {
        local_date: ctx.data.local_date,
        timezone_name: ctx.data.timezone_name,
        focus_note: root.querySelector('[data-intent-note]')?.value?.trim() || null,
        task_ids: ids,
        expected_version: Number(ctx.data.intent?.version || 0),
      }, { success: t('reflection.intentSaved') });
      setBusy(el, false);
    },
    async 'reflection-save-daily'(el, ctx) {
      const root = el.closest('[data-reflection-root]');
      setBusy(el, true);
      await change('reflection.upsert', `reflection-${ctx.data.local_date}`, {
        local_date: ctx.data.local_date,
        timezone_name: ctx.data.timezone_name,
        expected_version: Number(ctx.data.daily_reflection?.version || 0),
        ...readNotes(root, 'daily'),
      }, { success: t('reflection.dailySaved') });
      setBusy(el, false);
    },
    async 'reflection-save-weekly'(el, ctx) {
      const root = el.closest('[data-reflection-root]');
      setBusy(el, true);
      await change('weekly_review.upsert', `week-${ctx.data.week_starts_on}`, {
        week_starts_on: ctx.data.week_starts_on,
        timezone_name: ctx.data.timezone_name,
        expected_version: Number(ctx.data.weekly_review?.version || 0),
        ...readNotes(root, 'weekly'),
      }, { success: t('reflection.weeklySaved') });
      setBusy(el, false);
    },
    async 'reflection-accept-calibration'(el, ctx) {
      setBusy(el, true);
      await change('calibration.accept', 'calibration-hint', {
        multiplier: Number(el.dataset.multiplier),
        based_on_samples: Number(ctx.data.calibration?.sample_count || 0),
        expected_version: Number(ctx.data.calibration?.accepted?.version || 0),
      }, { success: t('reflection.calibrationSaved') });
      setBusy(el, false);
    },
  },
};
