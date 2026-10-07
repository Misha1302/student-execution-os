# ADR 0035 — Places as a product surface, location triggers and the routing provider (schema v32)

**Status:** Accepted

## Context

The travel owner (ADR 0008) already had `Place`, `CurrentLocationContext`,
`TravelEstimate` and a STAY/MOVE projection the planner reads, with a privacy rule that
default payloads do not carry exact positions. Users could not create or edit places,
put an event at one, say where they are, enter a travel time, ask for «когда приду
домой — напомни», or get travel times from a real provider.

## Decision

1. **Places stay the travel owner's** and gain create / rename / edit / delete as sync
   operations (`place.*`). Delete refuses while an open event, a hybrid option or an armed
   trigger uses the place; otherwise derived evidence (routes, current-location records,
   refresh state) goes with it and closed events keep their history without the place.
   Events get `event.update location_effect / arrival_requirement_minutes`.
2. **Privacy levels are separate things**:
   * *alias/name* — what lists, caches, operation results and the Assistant see;
   * *stored exact data* (address, map point) — read only by the owner's explicit edit
     view (`GET /api/v1/places/{id}`) and by the owner's phone for geofencing
     (`GET /api/v1/location-triggers/armed`);
   * *Assistant disclosure* — `PRIVATE_ALIAS` (name only, default) or `ASSISTANT_ADDRESS`
     (name and address, never coordinates);
   * *routing consent* — `routing_allowed`, required before a place's point is sent to a
     routing provider;
   * *current location* — a user statement (`location.set`) with a mandatory expiry
     (15 min – 24 h), never a permanent fact; the device position is read only when the
     user taps «Определить» on a place.
3. **Location triggers** are typed: one place, `ENTER` or `EXIT`, a title, one-shot or
   repeating (30-minute cool-down). No rule language. The server owns
   `ARMED → FIRED → DONE / CANCELLED` and de-duplicates firings; the Android phone detects
   crossings with the platform `LocationManager` proximity alerts (no Google Play services
   dependency, so de-Googled phones work and the verified Gradle dependency set is
   unchanged), shows the notification itself (offline too) and queues
   `location_trigger.fire` through WorkManager with an op id fixed per trigger, transition
   and ten-minute bucket. Alerts are restored after reboot/update, refreshed on start and
   on the alarm-sync push (sent when triggers change), and cleared on sign-out or account
   switch. A browser cannot watch location in the background; the UI says so.
4. **Routing provider boundary** (`travel/routing.py`): a `RoutingProvider` protocol, the
   `YandexDistanceMatrixProvider` adapter (transit/driving/walking in Russian cities;
   Google Maps Platform cannot be billed for Russian accounts), and a worker refresh that
   asks only for routes upcoming located events need, sends only the two points and the
   mode, uses bounded timeouts and one retry of the pure read, backs off per route, and
   stores ROUTING_PROVIDER `TravelEstimate`s with source revision, departure bucket,
   expected and policy-defined safe duration, and a 6-hour expiry. The planner still
   reads only `TravelEstimate`; a missing/failing/stale route keeps the plan UNKNOWN.
   The user's own valid estimate wins over the provider. The key comes from a deployment
   secret file and never reaches SQLite, a client, an error or a log line.
5. **Assistant**: `CREATE_PLACE`, `CREATE_LOCATION_TRIGGER`, and `CREATE_EVENT`
   `location_effect` are validated against the account's places; the model sees places by
   name only and can never send coordinates. An unknown place stays an unresolved field
   the user picks or creates from the card. RU/EN phrases («когда приду/уйду …», «добавь
   место …») are parsed offline (`agent/location_phrases.py` ⇄ `js/location-phrases.js`).

## Consequences

* Scenario «Дом → ВШЭ»: places, «я сейчас дома», a manual or provider route and an event at
  ВШЭ produce a travel block and «выйти до …» on Today; an expired location or no route is
  UNKNOWN, not a guess.
* The routing adapter is contract-tested with a mocked transport; live provider behaviour
  is not verified without a key. Geofence delivery is verified on an Android 15 emulator
  with mock GPS, not on a physical phone.
* Not done: imported class series cannot be given a place (their templates are
  source-owned); hybrid option selection remains an online request; current location is
  never set automatically from geofences.
