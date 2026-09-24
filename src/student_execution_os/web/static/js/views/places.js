import { load } from '../store.js';
import { t, code, fmtDateTime, fmtDuration } from '../i18n.js';
import { esc, icon, chip, kv, empty } from '../ui.js';

export default {
  id: 'places',
  tab: 'more',
  detail: true,
  title: () => t('nav.places'),
  load: ({ fresh }) => load('/api/v1/places', { fresh }),
  render(data) {
    const c = data.current_location || {};
    const unknown = c.state === 'UNKNOWN';
    const places = (data.places || []).map((p) => `<div class="row static">
      <span class="row-icon tone-canonical">${icon('place')}</span>
      <span class="row-main"><strong>${esc(p.alias || p.display_name)}</strong><small>${esc(p.alias ? p.display_name : '')}</small></span>
      ${chip(code('visibility', p.visibility_policy), 'muted')}
    </div>`).join('');
    const routes = (data.route_estimates || []).map((r) => `<div class="card">
      <div class="card-head"><div class="route">${icon('route')}<strong>${esc(r.origin)}</strong><span>→</span><strong>${esc(r.destination)}</strong></div>${chip(r.fresh ? t('places.fresh') : t('places.stale'), r.fresh ? 'ok' : 'warn')}</div>
      <dl class="kv-list">
        ${kv(t('places.expectedSafe'), `${fmtDuration(r.expected_duration_minutes)} / ${fmtDuration(r.safe_duration_minutes)}`)}
        ${kv(t('places.mode'), code('mode', r.transport_mode))}
        ${kv(t('places.calculated'), fmtDateTime(r.calculated_at))}
        ${kv(t('places.expires'), fmtDateTime(r.expires_at))}
      </dl>
    </div>`).join('');
    return `
      <section class="hero-status ${unknown ? 'status-unknown' : 'status-feasible'}">
        <div class="hero-icon ${unknown ? 'status-unknown' : 'status-feasible'}">${icon(unknown ? 'question' : 'place')}</div>
        <div class="hero-copy"><p class="eyebrow">${esc(t('places.current'))}</p>
          <h2>${esc(code('locState', c.state))}${c.place ? ` · ${esc(c.place)}` : ''}</h2>
          <p>${esc(unknown ? t('places.unknownHelp') : t('places.recorded', { when: fmtDateTime(c.recorded_at) }))}</p></div>
      </section>
      <section class="section"><div class="section-head"><h2>${esc(t('places.saved'))}</h2></div>${places ? `<div class="list">${places}</div>` : empty(t('places.none'), '', 'place')}</section>
      <section class="section"><div class="section-head"><h2>${esc(t('places.routes'))}</h2></div>${routes ? `<div class="stack">${routes}</div>` : `<p class="muted pad">${esc(t('places.noRoutes'))}</p>`}</section>
      <p class="help pad">${icon('alert')} ${esc(t('places.privacy'))}</p>`;
  },
};
