// Places on the device (schema v32, ADR 0035): create/edit/delete, «я сейчас …» with an
// expiry, a manual travel time, and location triggers. Every change is a queued sync
// operation. Exact address/position is loaded only when the user opens a place to edit
// it, and the device's position is read only when the user taps «Определить».
import { api } from './api.js';
import { peek } from './store.js';
import { t, fmtTime } from './i18n.js';
import { esc, icon, openSheet, chipGroup, chipValue, toast, confirmSheet, actionSheet, setBusy } from './ui.js';
import { change } from './actions.js';
import { newEntityId } from './sync.js';
import { isNative, refreshGeofences, requestLocation, geofenceStatus } from './native.js';

export const placeName = (p) => (p ? p.alias || p.display_name : '');
export const cachedPlaces = () => peek('/api/v1/places')?.places || [];

function readCoordinates(dialog) {
  const lat = dialog.querySelector('[data-place-lat]').value.trim();
  const lon = dialog.querySelector('[data-place-lon]').value.trim();
  if (!lat && !lon) return { latitude: null, longitude: null };
  const latitude = Number(lat.replace(',', '.'));
  const longitude = Number(lon.replace(',', '.'));
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || Math.abs(latitude) > 90 || Math.abs(longitude) > 180) {
    throw new Error(t('place.badCoordinates'));
  }
  return { latitude, longitude };
}

// One explicit read of the device position, for the place being edited (never in the background).
function locateInto(dialog, done = () => {}) {
  if (!navigator.geolocation) { toast(t('place.noGeolocation'), { error: true }); return; }
  const button = dialog.querySelector('[data-place-locate]');
  setBusy(button, true);
  navigator.geolocation.getCurrentPosition((position) => {
    dialog.querySelector('[data-place-lat]').value = position.coords.latitude.toFixed(6);
    dialog.querySelector('[data-place-lon]').value = position.coords.longitude.toFixed(6);
    setBusy(button, false);
    done();
  }, (err) => {
    setBusy(button, false);
    toast(t(err.code === 1 ? 'place.locationDenied' : 'place.locationFailed'), { error: true });
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
}

export async function placeSheet(existing = null, prefill = {}) {
  let detail = existing ? { ...existing } : { display_name: prefill.display_name || '', alias: '', address: prefill.address || '' };
  if (existing && (existing.has_address || existing.has_coordinates)) {
    try { detail = { ...detail, ...(await api(`/api/v1/places/${encodeURIComponent(existing.id)}`)) }; } catch {
      toast(t('place.exactOffline'));
    }
  }
  const dialog = openSheet({
    title: existing ? t('place.edit') : t('place.new'),
    // Progressive disclosure: name, address and «where I am» are the everyday flow; raw
    // coordinates and who may see/route the place are an advanced, safe-by-default part.
    body: `<label class="field"><span>${esc(t('place.name'))}</span><input data-place-name maxlength="120" value="${esc(detail.display_name || '')}" placeholder="${esc(t('place.namePlaceholder'))}"></label>
      <label class="field"><span>${esc(t('place.alias'))}</span><input data-place-alias maxlength="60" value="${esc(detail.alias || '')}" placeholder="${esc(t('place.aliasPlaceholder'))}"></label>
      <label class="field"><span>${esc(t('place.address'))}</span><input data-place-address maxlength="300" value="${esc(detail.address || '')}" autocomplete="off"></label>
      <div class="field"><span>${esc(t('place.position'))}</span>
        <p class="help" data-place-position-state></p>
        <button type="button" class="button small ghost" data-place-locate>${icon('place')}${esc(t('place.locate'))}</button>
        <small class="help">${esc(t('place.positionHelp'))}</small></div>
      <details class="details" data-place-advanced>
        <summary>${icon('chevron')}${esc(t('place.advanced'))}</summary>
        <div class="field"><span>${esc(t('place.coordinates'))}</span>
          <div class="field-row"><input data-place-lat inputmode="decimal" placeholder="${esc(t('place.lat'))}" aria-label="${esc(t('place.lat'))}" value="${esc(detail.latitude ?? '')}">
          <input data-place-lon inputmode="decimal" placeholder="${esc(t('place.lon'))}" aria-label="${esc(t('place.lon'))}" value="${esc(detail.longitude ?? '')}"></div></div>
        <div class="field"><span>${esc(t('place.assistantSees'))}</span>${chipGroup('place-visibility', [
          ['PRIVATE_ALIAS', t('place.visibility.PRIVATE_ALIAS')], ['ASSISTANT_ADDRESS', t('place.visibility.ASSISTANT_ADDRESS')]],
          detail.visibility_policy || 'PRIVATE_ALIAS')}</div>
        <label class="field toggle"><input type="checkbox" data-place-routing ${detail.routing_allowed ? 'checked' : ''}>
          <span>${esc(t('place.routing'))}</span></label>
        <p class="help">${esc(t('place.routingHelp'))}</p>
      </details>`,
    actions: `${existing ? `<button type="button" class="button danger ghost" data-place-delete>${esc(t('lifecycle.delete'))}</button>` : ''}
      <button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-place-save>${esc(t('common.save'))}</button>`,
  });
  const showPosition = () => {
    const has = dialog.querySelector('[data-place-lat]').value.trim() && dialog.querySelector('[data-place-lon]').value.trim();
    dialog.querySelector('[data-place-position-state]').textContent = t(has ? 'place.positionSet' : 'place.positionMissing');
  };
  showPosition();
  dialog.querySelectorAll('[data-place-lat],[data-place-lon]').forEach((input) => input.addEventListener('input', showPosition));
  dialog.querySelector('[data-place-locate]').addEventListener('click', () => locateInto(dialog, showPosition));
  dialog.querySelector('[data-place-delete]')?.addEventListener('click', async () => {
    dialog.close('delete');
    await deletePlace(existing);
  });
  dialog.querySelector('[data-place-save]').addEventListener('click', async () => {
    let coordinates;
    try { coordinates = readCoordinates(dialog); } catch (err) { toast(err.message, { error: true }); return; }
    const fields = {
      display_name: dialog.querySelector('[data-place-name]').value.trim(),
      alias: dialog.querySelector('[data-place-alias]').value.trim() || null,
      address: dialog.querySelector('[data-place-address]').value.trim() || null,
      visibility_policy: chipValue(dialog, 'place-visibility') || 'PRIVATE_ALIAS',
      routing_allowed: dialog.querySelector('[data-place-routing]').checked,
      ...coordinates,
    };
    if (!fields.display_name) { toast(t('place.needName'), { error: true }); return; }
    if (!existing) {
      if (await change('place.create', newEntityId('place'), fields, { success: t('place.created', { name: fields.alias || fields.display_name }) })) dialog.close('saved');
      return;
    }
    const changes = {};
    for (const [key, value] of Object.entries(fields)) if ((detail[key] ?? null) !== (value ?? null)) changes[key] = value;
    if ('latitude' in changes || 'longitude' in changes) { changes.latitude = fields.latitude; changes.longitude = fields.longitude; }
    dialog.close('saved');
    if (Object.keys(changes).length) await change('place.update', existing.id, changes, { success: t('place.saved') });
  });
  dialog.querySelector('[data-place-name]').focus();
  return dialog;
}

export async function deletePlace(place) {
  const uses = place.in_use || {};
  if ((uses.events || 0) + (uses.event_options || 0) + (uses.triggers || 0) > 0) {
    toast(t('place.inUse', { events: (uses.events || 0) + (uses.event_options || 0), triggers: uses.triggers || 0 }), { error: true });
    return;
  }
  const ok = await confirmSheet({ title: t('place.deleteTitle'), body: `<p>${esc(t('place.deleteBody', { name: placeName(place) }))}</p>`,
    confirmLabel: t('lifecycle.deleteConfirm'), cancelLabel: t('common.keep'), danger: true });
  if (ok) await change('place.delete', place.id, {}, { success: t('place.deleted') });
}

// «Я сейчас …»: a statement with an expiry, never a permanent fact.
export function whereAmISheet(places = cachedPlaces()) {
  const dialog = openSheet({
    title: t('place.whereNow'),
    body: `<div class="field"><span>${esc(t('place.iAmAt'))}</span>${chipGroup('where-place', [
      ...places.map((p) => [p.id, placeName(p)]), ['UNKNOWN', t('place.dontKnow')]], places[0]?.id || 'UNKNOWN')}</div>
      <div class="field"><span>${esc(t('place.forHowLong'))}</span>${chipGroup('where-for', [
        ['60', t('place.for1h')], ['180', t('place.for3h')], ['480', t('place.for8h')]], '180')}</div>
      <p class="help">${esc(t('place.whereHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-where-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-where-save]').addEventListener('click', async () => {
    const place = chipValue(dialog, 'where-place');
    const payload = place === 'UNKNOWN' ? { state: 'UNKNOWN' }
      : { state: 'KNOWN', place_id: place, expires_in_minutes: Number(chipValue(dialog, 'where-for') || 180) };
    dialog.close('saved');
    await change('location.set', '', payload, { success: place === 'UNKNOWN' ? t('place.whereUnknownSaved')
      : t('place.whereSaved', { name: placeName(places.find((p) => p.id === place)), until: fmtTime(new Date(Date.now() + payload.expires_in_minutes * 60000)) }) });
  });
}

export function routeSheet(places = cachedPlaces()) {
  if (places.length < 2) { toast(t('place.needTwo'), { error: true }); return; }
  const options = places.map((p) => [p.id, placeName(p)]);
  const dialog = openSheet({
    title: t('place.routeNew'),
    body: `<div class="field"><span>${esc(t('place.from'))}</span>${chipGroup('route-from', options, options[0][0])}</div>
      <div class="field"><span>${esc(t('place.to'))}</span>${chipGroup('route-to', options, options[1][0])}</div>
      <div class="field"><span>${esc(t('place.mode'))}</span>${chipGroup('route-mode', ['TRANSIT', 'WALK', 'DRIVE', 'BICYCLE'].map((m) => [m, t(`place.modes.${m}`)]), 'TRANSIT')}</div>
      <div class="field-row">
        <label class="field"><span>${esc(t('place.usually'))}</span><input type="number" min="1" max="1440" inputmode="numeric" data-route-expected></label>
        <label class="field"><span>${esc(t('place.withMargin'))}</span><input type="number" min="1" max="1440" inputmode="numeric" data-route-safe></label>
      </div>
      <p class="help">${esc(t('place.routeHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-route-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-route-save]').addEventListener('click', async () => {
    const from = chipValue(dialog, 'route-from');
    const to = chipValue(dialog, 'route-to');
    const expected = Number(dialog.querySelector('[data-route-expected]').value || 0);
    const safe = Number(dialog.querySelector('[data-route-safe]').value || 0) || expected;
    if (from === to) { toast(t('place.sameRoute'), { error: true }); return; }
    if (!expected) { toast(t('place.needMinutes'), { error: true }); return; }
    if (safe < expected) { toast(t('place.safeTooShort'), { error: true }); return; }
    dialog.close('saved');
    await change('travel.estimate.set', newEntityId('estimate'), { origin_place_id: from, destination_place_id: to,
      transport_mode: chipValue(dialog, 'route-mode'), expected_minutes: Math.round(expected), safe_minutes: Math.round(safe) },
    { success: t('place.routeSaved') });
  });
}

export function triggerSheet(prefill = {}, places = cachedPlaces()) {
  if (!places.length) { toast(t('trigger.needPlace'), { error: true }); return null; }
  const dialog = openSheet({
    title: t('trigger.new'),
    body: `<div class="field"><span>${esc(t('trigger.when'))}</span>${chipGroup('trigger-transition', [
      ['ENTER', t('trigger.enter')], ['EXIT', t('trigger.exit')]], prefill.transition || 'ENTER')}</div>
      <div class="field"><span>${esc(t('trigger.place'))}</span>${chipGroup('trigger-place', places.map((p) => [p.id, placeName(p)]), prefill.place_id || places[0].id)}</div>
      <label class="field"><span>${esc(t('trigger.what'))}</span><input data-trigger-title maxlength="300" value="${esc(prefill.title || '')}"></label>
      <label class="field toggle"><input type="checkbox" data-trigger-repeat><span>${esc(t('trigger.repeat'))}</span></label>
      <p class="help">${esc(t(isNative() ? 'trigger.androidHelp' : 'trigger.webHelp'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-trigger-save>${esc(t('compose.create'))}</button>`,
  });
  dialog.querySelector('[data-trigger-save]').addEventListener('click', async () => {
    const title = dialog.querySelector('[data-trigger-title]').value.trim();
    if (!title) { toast(t('checkin.needTitle'), { error: true }); return; }
    const payload = { place_id: chipValue(dialog, 'trigger-place'), transition: chipValue(dialog, 'trigger-transition'), title,
      repeat: dialog.querySelector('[data-trigger-repeat]').checked };
    const place = places.find((p) => p.id === payload.place_id);
    if (await change('location_trigger.create', newEntityId('trigger'), payload, {
      success: t(payload.transition === 'ENTER' ? 'trigger.createdEnter' : 'trigger.createdExit', { name: placeName(place) }) })) {
      dialog.close('saved');
      if (!place?.has_coordinates) toast(t('trigger.needsPosition', { name: placeName(place) }));
      await ensureLocationAccess();
    }
  });
  dialog.querySelector('[data-trigger-title]').focus();
  return dialog;
}

export async function triggerMenu(trigger) {
  const items = [];
  if (trigger.status === 'FIRED' || trigger.status === 'ARMED') items.push({ id: 'done', icon: 'check', label: t('trigger.done') });
  if (trigger.status === 'ARMED') items.push({ id: 'cancel', icon: 'x', label: t('trigger.cancel') });
  if (trigger.status !== 'ARMED') items.push({ id: 'reopen', icon: 'refresh', label: t('trigger.reopen') });
  items.push({ id: 'delete', icon: 'x', label: t('lifecycle.delete'), tone: 'danger' });
  const choice = await actionSheet({ title: trigger.title, items });
  const ops = { done: 'location_trigger.done', cancel: 'location_trigger.cancel', reopen: 'location_trigger.reopen', delete: 'location_trigger.delete' };
  if (choice && ops[choice]) await change(ops[choice], trigger.id, {}, { success: t(`trigger.toast.${choice}`) });
  refreshGeofences();
}

// On Android: ask for location only now that a place reminder exists, then «всегда».
export async function ensureLocationAccess() {
  if (!isNative()) return;
  let status = await geofenceStatus();
  if (status && !status.fine) status = await requestLocation(false);
  if (status && status.fine && !status.background) status = await requestLocation(true);
  if (status && !status.background) toast(t(status.fine ? 'trigger.onlyInUse' : 'trigger.denied'));
  refreshGeofences();
}

// What the phone can do about place reminders right now (Android only).
export async function locationAccessLine() {
  const status = await geofenceStatus();
  if (!status) return null;
  if (!status.fine) return { tone: 'warn', text: t('trigger.denied'), fix: true };
  if (!status.background) return { tone: 'warn', text: t('trigger.onlyInUse'), fix: true };
  if (!status.location_enabled) return { tone: 'warn', text: t('trigger.locationOff'), fix: false };
  return { tone: 'ok', text: t('trigger.watching', { n: status.watched }), fix: false };
}

// «Напомнить, когда приду: Дом» — never the schema words.
export const triggerLabel = (trigger) => t(trigger.transition === 'EXIT' ? 'trigger.labelExit' : 'trigger.labelEnter', { name: trigger.place_name || '…' });

// The event sheet's place choice: none, online, or one of the user's places.
export function placeSelect(effect = {}) {
  const places = cachedPlaces();
  const kind = effect.kind || 'NONE';
  if (kind === 'MOVE') return '';
  const selected = kind === 'STAY' ? effect.destination_place_id : kind;
  return `<label class="field"><span>${esc(t('place.eventWhere'))}</span><select data-e="place">
    <option value="NONE" ${selected === 'NONE' ? 'selected' : ''}>${esc(t('place.noPlace'))}</option>
    <option value="REMOTE" ${selected === 'REMOTE' ? 'selected' : ''}>${esc(t('place.online'))}</option>
    ${places.map((p) => `<option value="${esc(p.id)}" ${selected === p.id ? 'selected' : ''}>${esc(placeName(p))}</option>`).join('')}
  </select><small class="help">${esc(t('place.eventWhereHelp'))}</small></label>`;
}

export function readPlaceSelect(root) {
  const value = root.querySelector('[data-e="place"]')?.value;
  if (!value) return undefined;
  if (value === 'NONE' || value === 'REMOTE') return { kind: value };
  return { kind: 'STAY', destination_place_id: value };
}
