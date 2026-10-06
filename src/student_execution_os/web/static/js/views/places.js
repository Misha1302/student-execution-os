import { load, peek } from '../store.js';
import { t, code, fmtDateTime, fmtDuration, fmtTime } from '../i18n.js';
import { esc, icon, chip, empty, sectionHead } from '../ui.js';
import { placeSheet, whereAmISheet, routeSheet, triggerSheet, triggerMenu, triggerLabel, placeName } from '../places.js';
import { isNative } from '../native.js';

const TRIGGER_TONE = { ARMED: 'accent', FIRED: 'warn', DONE: 'ok', CANCELLED: 'muted' };

export default {
  id: 'places',
  tab: 'more',
  detail: true,
  title: () => t('nav.places'),
  load: ({ fresh }) => load('/api/v1/places', { fresh }),
  render(data) {
    const c = data.current_location || {};
    const unknown = c.state === 'UNKNOWN' || !c.place;
    const places = data.places || [];
    const triggers = data.location_triggers || [];
    const placeRows = places.map((p) => {
      const facts = [p.alias ? p.display_name : '', p.has_address ? t('place.hasAddress') : '',
        p.has_coordinates ? t('place.hasPosition') : t('place.noPosition'), p.routing_allowed ? t('place.routesAllowed') : '',
        p._pending ? t('checkin.pendingSync') : ''].filter(Boolean).join(' · ');
      return `<button class="row" data-action="place-edit" data-id="${esc(p.id)}">
        <span class="row-icon tone-canonical">${icon('place')}</span>
        <span class="row-main"><strong>${esc(placeName(p))}</strong><small>${esc(facts)}</small></span>
        ${chip(t(`place.visibility.short.${p.visibility_policy}`), 'muted')}
      </button>`;
    }).join('');
    const triggerRows = triggers.map((x) => `<div class="row static">
      <span class="row-icon tone-accent">${icon('bell')}</span>
      <span class="row-main"><strong>${esc(x.title)}</strong><small>${esc(triggerLabel(x))}${x.repeat ? ` · ${esc(t('trigger.repeating'))}` : ''}${x.needs_coordinates ? ` · ${esc(t('trigger.noPositionShort'))}` : ''}${x.fired_at ? ` · ${esc(t('trigger.firedAt', { when: fmtDateTime(x.fired_at) }))}` : ''}</small></span>
      <span class="row-aside">${chip(t(`trigger.status.${x.status}`), TRIGGER_TONE[x.status] || 'muted')}
        <button class="button small ghost" data-action="trigger-menu" data-id="${esc(x.id)}" aria-label="${esc(t('checkin.more', { title: x.title }))}">${esc(t('capture.more'))}</button></span>
    </div>`).join('');
    const routes = (data.route_estimates || []).map((r) => `<div class="card">
      <div class="card-head"><div class="route">${icon('route')}<strong>${esc(r.origin)}</strong><span>→</span><strong>${esc(r.destination)}</strong></div>${chip(r.fresh ? t('places.fresh') : t('places.stale'), r.fresh ? 'ok' : 'warn')}</div>
      <p class="muted">${esc(t('place.routeLine', { mode: t(`place.modes.${r.transport_mode}`), expected: fmtDuration(r.expected_duration_minutes), safe: fmtDuration(r.safe_duration_minutes) }))}</p>
      <small class="muted">${esc(t(r.source === 'USER_OVERRIDE' ? 'place.sourceUser' : r.source === 'ROUTING_PROVIDER' ? 'place.sourceProvider' : 'place.sourceOther'))} · ${esc(fmtDateTime(r.calculated_at))}${r.expires_at ? ` · ${esc(t('place.until', { when: fmtDateTime(r.expires_at) }))}` : ''}</small>
    </div>`).join('');
    return `
      <section class="hero-status ${unknown ? 'status-unknown' : 'status-feasible'}">
        <div class="hero-icon ${unknown ? 'status-unknown' : 'status-feasible'}">${icon(unknown ? 'question' : 'place')}</div>
        <div class="hero-copy"><p class="eyebrow">${esc(t('places.current'))}</p>
          <h2>${esc(unknown ? t('place.unknownNow') : c.place)}</h2>
          <p>${esc(unknown ? t('places.unknownHelp') : c.expires_at ? t('place.validUntil', { time: fmtTime(c.expires_at) }) : code('locState', c.state))}</p>
          ${places.length ? `<button class="button small" data-action="where-am-i">${esc(t('place.whereNow'))}</button>` : ''}</div>
      </section>
      <section class="section">${sectionHead(t('places.saved'), `<button class="button small" data-action="place-new">${icon('plus')}${esc(t('place.new'))}</button>`)}
        ${placeRows ? `<div class="list">${placeRows}</div>` : empty(t('places.none'), t('place.emptyHint'), 'place')}</section>
      <section class="section">${sectionHead(t('trigger.title'), places.length ? `<button class="button small" data-action="trigger-new">${icon('plus')}${esc(t('trigger.new'))}</button>` : '')}
        ${triggerRows ? `<div class="list">${triggerRows}</div>` : `<p class="muted pad">${esc(t('trigger.empty'))}</p>`}
        <p class="help pad">${esc(t(isNative() ? 'trigger.androidHelp' : 'trigger.webHelp'))}</p></section>
      <section class="section">${sectionHead(t('places.routes'), places.length > 1 ? `<button class="button small" data-action="route-new">${icon('plus')}${esc(t('place.routeNew'))}</button>` : '')}
        ${routes ? `<div class="stack">${routes}</div>` : `<p class="muted pad">${esc(t('places.noRoutes'))}</p>`}</section>
      <p class="help pad">${icon('alert')} ${esc(t('places.privacy'))}</p>`;
  },
  actions: {
    'place-new'() { placeSheet(); },
    'place-edit'(el) {
      const place = (peek('/api/v1/places')?.places || []).find((p) => p.id === el.dataset.id);
      if (place) placeSheet(place);
    },
    'where-am-i'() { whereAmISheet(); },
    'route-new'() { routeSheet(); },
    'trigger-new'() { triggerSheet(); },
    'trigger-menu'(el) {
      const trigger = (peek('/api/v1/places')?.location_triggers || []).find((x) => x.id === el.dataset.id);
      if (trigger) triggerMenu(trigger);
    },
  },
};
