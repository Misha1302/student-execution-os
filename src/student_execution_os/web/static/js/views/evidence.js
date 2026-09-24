import { load } from '../store.js';
import { t, code, fmtDateTime } from '../i18n.js';
import { esc, icon, chip, kv, empty } from '../ui.js';

const HEALTH_TONE = { CURRENT: 'ok', ACTIVE: 'ok', STALE: 'warn', DEGRADED: 'warn', UNAVAILABLE: 'danger', FAILED: 'danger' };

export default {
  id: 'evidence',
  tab: 'more',
  detail: true,
  title: () => t('nav.evidence'),
  load: ({ fresh }) => load('/api/v1/evidence', { fresh }),
  render(data) {
    const sources = (data.sources || []).map((s) => {
      const health = s.connector?.health_status || s.availability || 'ACTIVE';
      return `<div class="card">
        <div class="card-head"><div><p class="eyebrow">${esc(code('sourceKind', s.kind))}</p><h3>${esc(s.id)}</h3></div>${chip(code('health', health), HEALTH_TONE[health] || 'muted')}</div>
        ${s.connector ? `<dl class="kv-list">
          ${kv(t('ev.provider'), s.connector.provider)}
          ${kv(t('ev.lastSync'), fmtDateTime(s.connector.last_successful_complete_sync_at))}
        </dl>${s.connector.latest_failure_reason ? `<div class="banner warn">${icon('alert')}<div><p>${esc(s.connector.latest_failure_reason)}</p></div></div>` : ''}` : `<p class="muted">${esc(t('ev.noConnector'))}</p>`}
      </div>`;
    }).join('');
    const conflicts = (data.conflicts || []).filter((c) => c.status === 'OPEN').map((c) => `<div class="card card-danger">
      <div class="card-head"><div><p class="eyebrow">${esc(t('ev.openConflict'))}</p><h3>${esc(c.entity_ref)} · ${esc(c.field_path)}</h3></div>${chip(code('effective', 'CONFLICT'), 'danger')}</div>
      <dl class="kv-list">${kv(t('task.evidence'), (c.evidence_ids || []).join(', '))}${kv(t('task.policy'), c.policy_version)}</dl>
      <p class="help">${esc(t('task.conflictHelp'))}</p>
    </div>`).join('');
    const effective = (data.effective_fields || []).map((e) => `<div class="card">
      <div class="card-head"><h3>${esc(e.entity_ref)} · ${esc(e.field_path)}</h3>${chip(code('effective', e.state), e.state === 'CONFLICT' ? 'danger' : e.state === 'OVERRIDDEN' ? 'warn' : 'ok')}</div>
      <dl class="kv-list">
        ${kv(t('task.evidence'), (e.evidence_ids || []).join(', ') || '—')}
        ${kv(t('task.projection'), e.planning_projection?.at ? fmtDateTime(e.planning_projection.at) : '—')}
        ${e.reason ? kv(t('task.reason'), e.reason) : ''}
      </dl>
    </div>`).join('');
    const observations = (data.observations || []).slice(0, 30).map((o) => `<div class="row static">
      <span class="row-main"><strong>${esc(o.field_path)}</strong><small>${esc(o.source_system_id)} · ${esc(fmtDateTime(o.observed_at))}</small></span>
      ${chip(code('certainty', o.extraction_certainty), 'muted')}
    </div>`).join('');
    return `
      <p class="help pad">${esc(t('ev.intro'))}</p>
      <section class="section"><div class="section-head"><h2>${esc(t('ev.sources'))}</h2></div>${sources ? `<div class="stack">${sources}</div>` : empty(t('ev.noSources'), '', 'evidence')}</section>
      <section class="section"><div class="section-head"><h2>${esc(t('ev.conflicts'))}</h2></div>${conflicts ? `<div class="stack">${conflicts}</div>` : `<p class="muted pad">${esc(t('ev.noConflicts'))}</p>`}</section>
      <section class="section"><div class="section-head"><h2>${esc(t('ev.effective'))}</h2></div>${effective ? `<div class="stack">${effective}</div>` : `<p class="muted pad">${esc(t('ev.noEffective'))}</p>`}</section>
      <section class="section"><div class="section-head"><h2>${esc(t('ev.observations'))}</h2></div>${observations ? `<div class="list">${observations}</div>` : `<p class="muted pad">${esc(t('ev.noObservations'))}</p>`}</section>`;
  },
};
