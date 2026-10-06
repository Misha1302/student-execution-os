// Queued place / current-location / route / location-trigger operations projected onto
// the cached /api/v1/places model (schema v32). Pure; the server stays the owner. Exact
// addresses and positions are never part of this model (only has_address/has_coordinates).

const isPlaceOp = (x) => /^(place\.|location\.set|travel\.estimate\.set|location_trigger\.)/.test(x.operation?.type || '');

export function projectPlaces(data, ops) {
  const relevant = (ops || []).filter(isPlaceOp);
  if (!relevant.length || !data) return data;
  let places = [...(data.places || [])];
  let triggers = [...(data.location_triggers || [])];
  let routes = [...(data.route_estimates || [])];
  let current = { ...(data.current_location || {}) };
  let currentPlaceId = data.current_place_id ?? null;
  const nameOf = (id) => { const p = places.find((x) => x.id === id); return p ? (p.alias || p.display_name) : null; };
  for (const item of relevant) {
    const op = item.operation;
    const p = op.payload || {};
    const at = item.queued_at || new Date().toISOString();
    switch (op.type) {
      case 'place.create':
        if (!places.some((x) => x.id === op.entity_id)) {
          places.push({ id: op.entity_id, display_name: p.display_name, alias: p.alias || null, visibility_policy: p.visibility_policy || 'PRIVATE_ALIAS',
            routing_allowed: Boolean(p.routing_allowed), has_address: Boolean(p.address), has_coordinates: p.latitude != null,
            version: 0, in_use: { events: 0, event_options: 0, triggers: 0 }, _pending: true });
        }
        break;
      case 'place.update':
        places = places.map((x) => (x.id !== op.entity_id ? x : {
          ...x, ...Object.fromEntries(['display_name', 'alias', 'visibility_policy', 'routing_allowed'].filter((k) => k in p).map((k) => [k, p[k]])),
          ...('address' in p ? { has_address: Boolean(p.address) } : {}),
          ...('latitude' in p ? { has_coordinates: p.latitude != null } : {}),
          _pending: true,
        }));
        break;
      case 'place.delete':
        places = places.filter((x) => x.id !== op.entity_id);
        triggers = triggers.filter((x) => x.place_id !== op.entity_id);
        break;
      case 'location.set':
        currentPlaceId = p.state === 'UNKNOWN' ? null : p.place_id;
        current = { state: p.state || 'KNOWN', place: p.state === 'UNKNOWN' ? null : nameOf(p.place_id), recorded_at: at,
          expires_at: p.expires_in_minutes ? new Date(new Date(at).getTime() + p.expires_in_minutes * 60000).toISOString() : null,
          source: 'USER_MANUAL', _pending: true };
        break;
      case 'travel.estimate.set':
        if (!routes.some((x) => x.id === op.entity_id)) {
          routes = [{ id: op.entity_id, origin: nameOf(p.origin_place_id), destination: nameOf(p.destination_place_id),
            transport_mode: p.transport_mode || 'TRANSIT', expected_duration_minutes: p.expected_minutes,
            safe_duration_minutes: p.safe_minutes ?? p.expected_minutes, source: 'USER_OVERRIDE', calculated_at: at,
            expires_at: null, fresh: true, _pending: true }, ...routes];
        }
        break;
      case 'location_trigger.create':
        if (!triggers.some((x) => x.id === op.entity_id)) {
          const place = places.find((x) => x.id === p.place_id);
          triggers.unshift({ kind: 'LOCATION_TRIGGER', id: op.entity_id, place_id: p.place_id, transition: p.transition || 'ENTER',
            title: p.title, note: p.note || null, radius_meters: p.radius_meters || 150, repeat: Boolean(p.repeat), status: 'ARMED',
            fired_at: null, fire_count: 0, place_name: nameOf(p.place_id), needs_coordinates: !place?.has_coordinates, version: 0, _pending: true });
        }
        break;
      default: {
        const status = { 'location_trigger.done': 'DONE', 'location_trigger.cancel': 'CANCELLED', 'location_trigger.reopen': 'ARMED' }[op.type];
        if (op.type === 'location_trigger.delete') triggers = triggers.filter((x) => x.id !== op.entity_id);
        else if (status) triggers = triggers.map((x) => (x.id === op.entity_id ? { ...x, status, _pending: true } : x));
        else if (op.type === 'location_trigger.fire') {
          triggers = triggers.map((x) => (x.id === op.entity_id && x.status === 'ARMED' && x.transition === p.transition
            ? { ...x, status: x.repeat ? 'ARMED' : 'FIRED', fired_at: p.occurred_at || at, fire_count: (x.fire_count || 0) + 1, _pending: true } : x));
        } else if (op.type === 'location_trigger.update') {
          triggers = triggers.map((x) => (x.id === op.entity_id ? { ...x, ...p, ...(p.place_id ? { place_name: nameOf(p.place_id) } : {}), _pending: true } : x));
        }
      }
    }
  }
  return { ...data, places, location_triggers: triggers, route_estimates: routes, current_location: current, current_place_id: currentPlaceId };
}
