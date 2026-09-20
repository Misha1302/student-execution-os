# ADR 0008 — Travel-aware planning ownership and feasibility

- Status: Accepted for Pass 7
- Date: 2026-09-21

## Context

SPEC v2.1 distinguishes three things that must not collapse into one mutable object:

1. canonical user-owned location/journey facts such as a booked train;
2. route-provider estimates and current-location context used as planning inputs;
3. planner-derived commute/buffer blocks.

Pass 7 must make travel feasibility-material without turning every commute into canonical truth or silently guessing missing origin/route state.

The executable acceptance target is AT-33 through AT-37.

## Decision

### Canonical ownership

Canonical Event remains the owner of booked/fixed journey state.

A booked train/flight is represented as a normal Event with:

- `LocationEffectKind.MOVE`;
- canonical origin place id;
- canonical destination place id;
- fixed interval.

Planner adjacency never deletes or rewrites that Event.

Pass 7 also introduces account-scoped Place rows for server-owned local place identity and private location fields. New STAY/MOVE Events validate referenced Place ids against the Event account and fail closed on missing/cross-account ids.

### Planning-input ownership

`CurrentLocationContext` is a versioned planning input history, not an inferred permanent fact.

Its state is:

- `KNOWN`;
- `ASSUMED`;
- `UNKNOWN`.

If the effective origin is unknown when an admitted origin can change required travel or hard feasibility, planning fails closed with `UNKNOWN`.

`TravelEstimate` is source-backed route evidence/history with:

- origin/destination;
- mode;
- expected duration;
- safe duration;
- source and source revision;
- calculation time;
- optional expiry.

Expired evidence is not silently reused as safe routing input.

### Derived commute

`TravelProjectionBuilder` walks required location-bearing Events chronologically from the effective current location.

Rules:

- `NONE` and `REMOTE` do not change physical location;
- `STAY(destination)` requires presence at that destination and leaves location there after the Event;
- `MOVE(origin,destination)` is the canonical journey itself: the planner may derive travel to its origin if needed, but never derives a duplicate origin→destination commute for the booked journey; after the MOVE Event, current location becomes its canonical destination;
- after an appointment the next commute targets the actual next location-bound item; there is no automatic return to a previous origin.

For a required transition:

`latest_safe_departure = event_start - arrival_requirement - safe_travel_duration`.

AT-33 therefore produces:

- Event start 16:00;
- safe travel 45 minutes;
- arrival requirement 10 minutes;
- latest safe departure 15:05.

### Hard feasibility

Travel is not a UI-only projection.

The immutable `PlanningSnapshot` includes the derived `TravelProjection`, and its input hash includes:

- travel interval;
- arrival buffer;
- route estimate identity;
- origin/destination;
- target Event;
- unknown/infeasible travel reasons.

The feasibility engine inserts derived travel + arrival buffers into hard occupied time before constructive/exact search.

The independent witness validator checks candidate WORK blocks against the same travel occupancy.

Therefore a workload that only fits when commute is ignored cannot be reported FEASIBLE.

### Unknown vs infeasible

Travel projection separates uncertainty from contradiction.

Examples yielding `UNKNOWN`:

- feasibility-material current origin is unknown;
- required route estimate is missing;
- only stale/expired route evidence exists.

Examples yielding `INFEASIBLE`:

- required travel would have to start before the analysis horizon/current planning boundary;
- derived travel/buffer overlaps another required Event;
- derived travel/buffer conflicts with a hard user time constraint;
- exact search proves no legal work witness after commute occupancy is included.

Heuristic/search-budget failure remains `UNKNOWN`; Pass 7 does not reinterpret budget exhaustion as impossibility.

### Derived PlanBlocks

Pass 7 adds derived block types:

- `TRAVEL_TRANSITION`;
- `BUFFER`.

A travel block carries the source Event id and TravelEstimate id.

Migration v6 rebuilds the persisted `plan_blocks` table so old WORK/EVENT_PROJECTION history survives and new travel blocks round-trip through `SQLitePlanStore`.

### Arrival requirement

Event gains `arrival_requirement_minutes >= 0`.

For a derived travel transition the arrival requirement becomes a hard BUFFER immediately before the Event, and travel ends at the start of that buffer.

Pass 7 does not add a generic pre-event buffer policy beyond this travel/arrival requirement contract.

## Schema

SQLite schema v6 adds:

- `events.arrival_requirement_minutes`;
- `places`;
- `current_location_context`;
- `travel_estimates`;
- expanded `plan_blocks` support for TRAVEL_TRANSITION / BUFFER / travel_estimate_id.

Migration coverage verifies v5 → v6 while preserving pre-existing PlanSnapshot/PlanBlock history.

## Verification contract

Executable coverage includes:

- AT-33 — HSE → psychologist: 45m safe route + 10m arrival => 15:05 latest-safe-departure;
- AT-34 — unknown origin => UNKNOWN, no fabricated route;
- AT-35 — next commute uses actual next location; no fake return;
- AT-36 — booked MOVE Event remains canonical through replanning;
- AT-37 — expired route evidence => UNKNOWN until admissible fresh route exists;
- travel occupancy can turn an otherwise feasible work day into proven INFEASIBLE;
- new travel PlanBlocks survive plan save/load;
- v5 plan history survives schema v6 migration;
- `travel-smoke` is part of Makefile and GitHub Actions.

## Strongest alternative considered

An alternative is to materialize every commute as a canonical Event whenever adjacency changes.

That is rejected because adjacency is planner-derived state. Canonicalizing it would make replanning mutate user truth, blur the difference between a booked journey and a suggested commute, and create deletion/reconciliation problems whenever adjacent items change.

The chosen model keeps booked journeys canonical and ordinary commute derived.

## Scope intentionally deferred

Pass 7 does not implement:

- a live maps/routing provider connector;
- live traffic refresh/webhooks;
- multi-modal route optimization;
- automatic learned commuting distributions;
- task-level location semantics;
- recurrence;
- notifications;
- offline replication.

The travel repository accepts provider/user/fallback estimates through the established planning-input boundary; a real routing provider can be added later without changing canonical ownership.
