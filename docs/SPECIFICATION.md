# Student Execution OS — Normative Product and System Specification

**Version:** 1.0  
**Status:** normative baseline  
**Purpose:** define the product, domain model, API boundaries, capture/reconciliation semantics, planning model, travel constraints, risk engine, notifications, sync, privacy/security boundaries, MVP phases, and acceptance criteria precisely enough that implementation teams do not need to reinvent core semantics.

## 0. Normative language

- **MUST** — mandatory requirement.
- **MUST NOT** — prohibited behavior.
- **SHOULD** — default behavior; deviation requires a documented reason.
- **SHOULD NOT** — discouraged behavior.
- **MAY** — optional behavior.

An implementation that violates a MUST/MUST NOT requirement is non-conformant.

---

## 1. Product definition

The product is not a generic TODO list. Its job is to transform fragmented obligations, fixed events, deadlines, personal plans, location/travel constraints, and available calendar capacity into a trustworthy workload model and a small executable plan.

Canonical pipeline:

```text
SOURCES
  ↓
CAPTURE
  ↓
NORMALIZATION
  ↓
PROVENANCE + DEDUPLICATION
  ↓
RECONCILIATION
  ↓
CANONICAL WORKLOAD MODEL
  ↓
CAPACITY + LOCATION + TRAVEL
  ↓
RISK ENGINE
  ↓
PLANNER
  ↓
EXECUTION QUEUE
  ↓
REMINDERS / USER ACTION
  ↓
FEEDBACK
  └──────────────→ recalculation
```

The product should answer:

1. What do I need to do?
2. What can I realistically complete?
3. What should I do now?
4. What changed in external sources?
5. Can I trust the local model?

---

## 2. Fundamental architecture

### 2.1 Server is canonical state

The server MUST own canonical state.

```text
Mobile ────────┐
Web ───────────┤
Desktop ───────┤
LLM Skill ─────┼──→ Server → Canonical DB
LMS Connector ─┤
Calendar ──────┤
Other Sources ─┘
```

Clients MAY cache state locally. A client, LLM, or connector MUST NOT become an independent source of truth.

### 2.2 Source != Extractor != Actor

The system MUST distinguish:

- **Source** — where a fact came from.
- **Extractor** — software/model that parsed the source.
- **Actor** — principal that requested a mutation.

Example:

```text
User: "Во вторник сдать линал"
Source    = USER_UTTERANCE
Extractor = CHATGPT
Actor     = USER_VIA_CHATGPT
```

If an LLM parses a PDF:

```text
Source    = syllabus.pdf
Extractor = LLM
Actor     = IMPORT_PIPELINE
```

An LLM MUST NOT automatically be recorded as the source of facts it merely extracted.

---

## 3. Core domain model

The system MUST NOT represent every concept as a single `Task`.

```text
Obligation
├── Task
├── Event
└── Project
```

Additional canonical concepts:

- `Milestone`
- `ScheduleBlock`
- `Place`
- `TravelEstimate`
- `SourceRecord`
- `Observation`
- `CandidateObligation`
- `Conflict`
- `Plan`
- `Notification`
- `RecurringTemplate`

### 3.1 Obligation

```text
Obligation
    id: UUID

    kind:
        TASK
        EVENT
        PROJECT

    category:
        HOMEWORK
        LESSON
        EXAM
        MEETING
        PERSONAL_APPOINTMENT
        WORK
        ADMIN
        ERRAND
        PERSONAL
        GENERAL
        CUSTOM

    title: string
    description: string?

    status:
        DRAFT
        ACTIVE
        COMPLETED
        CANCELLED
        ARCHIVED

    importance:
        LOW
        NORMAL
        HIGH
        CRITICAL

    created_at
    updated_at
    completed_at?
    version: integer
```

`kind` controls structural behavior. `category` is a semantic subtype for UI, filters, and policy.

Examples:

```text
Homework:   kind = TASK,  category = HOMEWORK
Lesson:     kind = EVENT, category = LESSON
Psychologist: kind = EVENT, category = PERSONAL_APPOINTMENT
```

---

## 4. Time model

The core MUST distinguish at least five temporal concepts.

### 4.1 actual_cutoff

External real cutoff after which an objective consequence occurs.

Example: LMS submission closes at `2026-09-23T23:59`.

The system MUST NOT automatically move `actual_cutoff`.

### 4.2 target_at

User's desired completion time.

Example: finish Tuesday even though the actual cutoff is Thursday.

`target_at` MAY be rescheduled. Rescheduling target MUST NOT change `actual_cutoff`.

### 4.3 actionable_from

Earliest instant at which work can actually begin. Planner MUST NOT schedule work before `actionable_from` when work is physically unavailable before it.

### 4.4 fixed event interval

Events use `starts_at` and `ends_at`. Example: psychologist `16:00–17:00`.

This is occupied time, not a deadline.

### 4.5 scheduled work

Work is materialized through `ScheduleBlock` objects. A deadline and scheduled work MUST remain separate concepts.

### 4.6 Hard/soft terminology

The UI MAY expose "hard" and "soft" deadlines, but the core MUST store richer semantics:

```text
actual_cutoff
target_at
actionable_from
planned_work[]
```

A hard deadline approximates `actual_cutoff`. A soft deadline normally corresponds to `target_at` without an external cutoff, or to a `target_at` earlier than an external cutoff.

---

## 5. Task and effort

A `Task` represents work requiring effort.

```text
Task
    obligation_id
    effort:
        estimate
        remaining
    splittable: bool
    min_chunk_duration?
    max_chunk_duration?
    preferred_time_windows[]?
    required_location?
    allowed_locations[]?
    actionable_from?
    actual_cutoff?
    target_at?
```

The system MUST distinguish `estimated_total_effort` from `remaining_effort`.

The user MUST NOT be forced to enter exact minutes. Quick estimation mode:

```text
QUICK    ≈ 15–30 min
SMALL    ≈ 30–90 min
MEDIUM   ≈ 1.5–3 h
LARGE    ≈ 3–6 h
PROJECT  ≈ multi-session
```

Precise mode MAY store an expected duration and uncertainty range. Historical personalized priors MAY be learned later.

---

## 6. Event

An `Event` occupies a fixed or constrained time interval.

```text
Event
    obligation_id
    starts_at
    ends_at
    time_flexibility:
        FIXED
        FLEXIBLE_WINDOW
    earliest_start?
    latest_end?
    location_id?
    arrival_buffer?
    departure_buffer?
    preparation_buffer?
    post_event_buffer?
    location_mode:
        PHYSICAL
        REMOTE
        HYBRID
        NONE
```

Remote events require no physical travel. Hybrid events MAY require choosing an occurrence mode.

---

## 7. Project and milestones

A `Project` represents a larger obligation composed of milestones and/or child obligations.

```text
Project
    obligation_id
    milestones[]
    child_obligations[]
```

One obligation MAY have multiple milestones.

```text
Milestone
    id
    obligation_id
    title
    actionable_from?
    target_at?
    actual_cutoff?
    consequence?
    status
```

The core MUST NOT assume `Task -> exactly one deadline`.

---

## 8. Places and location privacy

```text
Place
    id
    alias:
        HOME
        HSE
        DUBKI
        PSYCHOLOGIST
        CUSTOM
    display_name
    coordinates?
    address?
    visibility_policy
```

LLM-facing tools SHOULD prefer opaque aliases/IDs such as `HOME`, `HSE`, or `DUBKI` instead of exact addresses. Exact coordinates SHOULD remain inside the routing/server boundary unless a granted capability requires disclosure.

---

## 9. Travel model

Travel MUST NOT be stored as a fixed property of an event and MUST NOT be duplicated as ordinary TODO tasks.

Correct model:

```text
location A
    ↓
Travel
    ↓
location B
```

### 9.1 TravelEstimate

```text
TravelEstimate
    origin_place_id
    destination_place_id
    departure_at
    transport_mode:
        WALK
        PUBLIC_TRANSPORT
        CAR
        TAXI
        BIKE
        CUSTOM
    expected_duration
    safe_duration
    source:
        ROUTING_PROVIDER
        USER_OVERRIDE
        LEARNED
        FALLBACK
    calculated_at
    expires_at?
```

Planning SHOULD use `safe_duration` by default.

### 9.2 User overrides

Users MAY define known travel rules, e.g.:

```text
HSE → PSYCHOLOGIST = 45m safe
DUBKI → PSYCHOLOGIST = 2h safe
```

Manual overrides SHOULD outrank generic fallback estimates unless policy explicitly changes.

### 9.3 No assumed return trip

After an event, the planner MUST NOT automatically create a return trip to the origin. Travel is recomputed between consecutive location-bound blocks.

---

## 10. ScheduleBlock

A plan is composed from schedule blocks.

```text
ScheduleBlock
    id
    type:
        FIXED_EVENT
        WORK
        TRAVEL
        BUFFER
        PERSONAL_BLOCK
        UNAVAILABLE
    starts_at
    ends_at
    obligation_id?
    from_place_id?
    to_place_id?
    generated_by:
        USER
        PLANNER
        CONNECTOR
    locked: bool
    plan_revision
```

- `FIXED_EVENT` represents a fixed event.
- `WORK` represents scheduled work; its end MUST NOT automatically mark the task complete.
- `TRAVEL` is generated between location-bound blocks and may be invalidated when neighbors change.
- `BUFFER` is used for early arrival, preparation, parking, uncertainty, or recovery.
- Locked blocks MUST NOT be moved automatically; conflicts must be surfaced instead.

---

## 11. Capacity and slack

For any interval the system SHOULD calculate `available_capacity`, accounting for fixed events, travel, locked blocks, unavailable periods, sleep policy, and allowed work windows.

For a task:

```text
slack = available_capacity_before_cutoff - remaining_effort
```

The system MUST NOT substitute simple wall-clock difference `cutoff - now` for usable capacity.

---

## 12. Risk engine

Risk states:

```text
UNKNOWN
SAFE
START_SOON
AT_RISK
CRITICAL
IMPOSSIBLE
OVERDUE
```

- `UNKNOWN`: critical data missing.
- `SAFE`: adequate capacity and margin.
- `START_SOON`: safe margin will materially deteriorate if work does not begin soon.
- `AT_RISK`: still possible, but capacity is tight or sensitive to disruption.
- `CRITICAL`: extremely low slack.
- `IMPOSSIBLE`: remaining work cannot fit before cutoff under current hard constraints.
- `OVERDUE`: external cutoff passed and obligation incomplete.

Risk MUST be recomputed when effort, capacity, cutoff, calendar, travel requirements, or plan changes.

Risk transitions SHOULD be retained for analytics and notification suppression.

---

## 13. Latest Safe Start

A Task SHOULD expose `latest_safe_start`: the latest feasible point at which remaining work can still fit into usable capacity with configured safety margin.

It is not necessarily `deadline - duration`, because the interval may contain classes, sleep, travel, and unavailable time.

---

## 14. Latest Safe Departure

For a location-bound Event:

```text
latest_safe_departure = event_start - arrival_buffer - safe_travel_duration
```

Example:

```text
Psychologist starts: 16:00
HSE → psychologist safe travel: 45m
arrival buffer: 10m
latest safe departure = 15:05
```

If origin is uncertain, the planner MUST preserve uncertainty rather than invent a location.

```text
IF origin = HSE:   depart <= 15:05
IF origin = DUBKI: depart <= 13:50
IF origin = HOME:  depart <= 16:00 - travel(HOME, PSYCHOLOGIST) - buffer
```

---

## 15. Location uncertainty

Location context may be `KNOWN`, `ASSUMED`, or `UNKNOWN`.

If feasibility materially depends on origin and origin is unknown, the system SHOULD surface a question/conditional plan instead of fabricating a route.

Location observations MUST have freshness; a past statement such as "I am at HSE" must not remain indefinitely authoritative.

---

## 16. Psychologist example

Input:

```text
Psychologist 16:00–17:00
location = PSYCHOLOGIST
arrival_buffer = 10m
HSE → PSYCHOLOGIST = 45m safe
DUBKI → PSYCHOLOGIST = 2h safe
```

If the previous fixed block ends at HSE at 14:30:

```text
14:30–15:05 available
15:05–15:50 travel
15:50–16:00 buffer
16:00–17:00 psychologist
```

If a previous HSE event runs `15:00–15:40`, while latest safe departure is `15:05`, the planner MUST report a conflict rather than pretend the plan is feasible.

---

## 17. Importance, urgency, risk, and colors

These concepts MUST be distinct.

- **Importance** — consequence/value.
- **Urgency** — how soon action is required.
- **Risk** — degree to which the current plan threatens success.

Manual importance values:

```text
LOW
NORMAL
HIGH
CRITICAL
```

Importance MUST NOT automatically rise merely because a deadline approaches.

UI MAY retain colors:

```text
TRANSPARENT
GREEN
YELLOW
RED
BURNING
```

But by default colors SHOULD visualize computed execution risk rather than five manually maintained urgency fields.

Illustrative mapping:

```text
TRANSPARENT -> no immediate urgency
GREEN       -> SAFE
YELLOW      -> START_SOON
RED         -> AT_RISK / CRITICAL
BURNING     -> IMPOSSIBLE / OVERDUE / immediate critical action
```

Mapping MUST be configurable. A manual display floor MAY exist, but UI MUST distinguish computed state from manual override.

---

## 18. Anti-starvation

Tasks without an external cutoff must not disappear forever.

Anti-starvation MAY use age, number of deferrals, importance, time since progress, and target date. Repeated deferral is a risk signal, not punishment.

---

## 19. Execution Queue

The default experience SHOULD avoid presenting the full backlog as the primary decision interface.

The server SHOULD expose `get_next_actions()`, normally returning about 1–5 actions.

Each action SHOULD include:

```text
what
recommended_duration
why_now
risk_if_skipped
location/context
```

Every automatic recommendation MUST be explainable.

---

## 20. Explainability

Automatic recommendations and risk evaluations MUST carry:

```text
reason_code
human_readable_reason
relevant_factors
```

The system MUST preserve which values came from real observations and which are defaults or estimates.

---

## 21. Sorting

Supported views MAY include:

```text
AUTO
DEADLINE
RISK
IMPORTANCE
CREATED_AT
MANUAL
```

AUTO MUST be deterministic and may consider computed risk, importance, actual cutoff, slack, target, and anti-starvation signals. Tie-breaking must be stable.

---

## 22. Capture layer

All potential obligations SHOULD enter through a unified capture boundary.

Sources may include manual UI, LLM, SmartLMS, Canvas, Moodle, calendar, email, Telegram, PDF/syllabus, and future connectors.

Capture does not imply immediate canonical commit.

---

## 23. CandidateObligation

```text
CandidateObligation
    id
    proposed_fields
    source_record_id
    extractor
    field_confidence
    authority
    deduplication_key?
    status:
        PENDING
        AUTO_ACCEPTED
        ACCEPTED
        REJECTED
        MERGED
        CONFLICT
```

Confidence MUST be field-level. A low-confidence field MUST NOT invalidate all other extracted information.

---

## 24. Confidence vs authority

**Confidence** asks whether the extractor probably parsed the value correctly.

**Authority** asks whether the source should determine the canonical fact.

Example:

```text
SmartLMS deadline: confidence=0.99, authority=AUTHORITATIVE
Group-chat rumor: confidence=0.99, authority=LOW
User target: confidence=0.99, authority(target_at)=AUTHORITATIVE_USER
```

---

## 25. SourceRecord and Observation

```text
SourceRecord
    id
    source_type
    external_id?
    source_uri?
    source_revision?
    observed_at
    content_hash?
    metadata
    connector_id?
```

Private raw source content SHOULD NOT be retained without necessity.

```text
Observation
    entity_id?
    candidate_id?
    field
    value
    source_record_id
    authority
    confidence
    observed_at
    extractor
```

The system should be able to answer: "Who says this deadline is correct?"

---

## 26. Commit policy and critical fields

Avoid confirmation fatigue.

- Explicit user command MAY commit directly.
- High-confidence authoritative import MAY auto-accept according to policy.
- LLM inference of a critical field without adequate evidence creates a pending candidate.
- Contradictory observations create a conflict unless an explicit deterministic policy resolves them safely.

Critical fields include at least actual cutoff, fixed event start/end, location when travel feasibility depends on it, completion, and cancellation.

Low-confidence critical fields MUST NOT silently become canonical.

---

## 27. Deduplication

The system MUST prevent duplicates from repeated retries and overlapping sources.

Signals MAY include external source ID, course/context, title similarity, deadline proximity, source URL, event interval, and semantic fingerprint.

Automatic merge is prohibited when confidence is insufficient.

---

## 28. Reconciliation and change detection

The reconciler combines observations into canonical state.

Example:

```text
Teacher message: deadline = Wednesday
SmartLMS:        deadline = Thursday
```

The system MUST NOT silently overwrite without policy/evidence.

Source changes MUST be recorded and inspectable.

If user has `target_at`, moving external `actual_cutoff` MUST NOT automatically move the target.

Submission/completion reconciliation MUST respect source semantics; `submitted` is not universally equivalent to fully complete.

---

## 29. LLM integration

LLMs are natural-language interfaces and extractors.

An LLM MUST NOT:

- own canonical state;
- implement the authoritative risk algorithm;
- bypass reconciliation;
- write canonical storage directly;
- bypass permissions;
- define final scheduling semantics outside server policy.

Suggested MVP tools:

```text
capture_obligation
get_obligation
list_obligations
update_obligation
complete_obligation
cancel_obligation
get_schedule
get_next_actions
get_risk
create_place_alias
set_travel_override
```

Server pipeline:

```text
LLM
↓
API request
↓
schema validation
↓
permission validation
↓
normalization
↓
deduplication
↓
reconciliation
↓
commit or candidate
↓
response
```

---

## 30. Idempotency

Every create/capture mutation MUST support an idempotency key or client request ID.

Repeated retries of one logical operation MUST produce one logical obligation.

---

## 31. API architecture

Base namespace:

```text
/api/v1
```

Breaking contract changes require a new major version.

Suggested domains:

```text
/auth
/capture
/candidates
/obligations
/projects
/milestones
/places
/travel
/calendar
/plans
/risk
/execution
/notifications
/sources
/conflicts
/sync
/events
```

Core endpoints SHOULD cover obligation lifecycle, capture, candidate resolution, planning/risk, travel overrides, and conflict resolution.

Public schemas SHOULD use OpenAPI 3.1 or equivalent. LLM tool definitions SHOULD be generated from or validated against the same canonical schemas.

---

## 32. Concurrency and sync

Canonical entities have a version token.

Clients SHOULD provide `expected_version` or HTTP `If-Match` for mutation. Stale mutations MUST fail as conflicts instead of silently overwriting newer state.

Clients SHOULD use delta sync with a monotonic server change cursor distinct from per-entity version.

Offline mutations SHOULD preserve:

```text
client_mutation_id
base_entity_version
local_timestamp
operation
```

Blind last-write-wins MUST NOT be the default conflict strategy.

---

## 33. Domain events

Core SHOULD emit at least:

```text
ObligationCreated
ObligationUpdated
ObligationCompleted
ObligationCancelled
CandidateCreated
DeadlineObserved
DeadlineChanged
ConflictCreated
ConflictResolved
EventScheduled
PlanRecalculated
PlanBecameInfeasible
RiskChanged
TravelEstimateChanged
ReminderScheduled
ReminderDelivered
ReminderFailed
```

Event consumers MUST be idempotent because duplicate delivery is allowed.

---

## 34. Connectors

Connectors MUST NOT directly write canonical storage.

```text
fetch
↓
normalize external records
↓
emit SourceRecords + Observations
↓
capture/reconciliation
```

Conceptual interface:

```text
Connector
    authenticate()
    poll(cursor)
    normalize(raw)
    emit_observations()
    checkpoint()
```

Incremental connectors SHOULD persist cursors/checkpoints and use least-privilege permissions.

---

## 35. Extensibility

Core SHOULD define internal extension points for:

```text
Connector
RoutingProvider
NotificationChannel
RiskStrategy
PlanningPolicy
```

A plugin marketplace and mature public third-party ecosystem are NOT MVP requirements.

---

## 36. Notifications

Notifications SHOULD reflect meaningful state transitions rather than fixed spam.

Useful triggers include upcoming event, latest safe departure, latest safe start, deadline warning, risk escalation, plan conflict, source changed, and completion check.

Logical notifications MUST be idempotent and revision-aware. Stale notifications MUST be suppressed.

A completion follow-up such as `Ты это сделал? [Да] [Нет] [Перенести]` MAY exist but MUST NOT be sent unless the initial reminder was actually delivered.

`SNOOZE` changes notification time only; it MUST NOT mutate actual cutoff, target, or fixed event time. Rescheduling is a separate operation.

---

## 37. Quiet hours and timezones

The system SHOULD support quiet hours and an IANA timezone.

Absolute timestamps MUST be stored in UTC. Recurring local-time semantics MUST preserve timezone identity. DST behavior MUST be tested.

---

## 38. Planner

Planner inputs include obligations, fixed events, calendar availability, locations, travel estimates, remaining effort, deadlines/targets, and user constraints/preferences.

Output:

```text
Plan
    id
    revision
    horizon_start
    horizon_end
    blocks[]
    generated_at
    feasibility_status
    explanation
```

Recalculation SHOULD occur when obligations, deadlines, events, progress, calendar, location assumptions, travel estimates, locks, or explicit user requests materially change inputs.

---

## 39. Constraints vs preferences

Hard constraints include fixed events, actual cutoffs, actionable-from, remaining effort, availability, travel, locked blocks, chunk limits, splittability, and hard buffers.

Preferences MAY include preferred study hours, block length, gap minimization, and preferred work location.

Planner MAY violate preferences to satisfy hard constraints but SHOULD explain meaningful violations.

---

## 40. Feasibility

Every plan MUST expose:

```text
FEASIBLE
AT_RISK
INFEASIBLE
UNKNOWN
```

If infeasible, planner MUST NOT create the illusion of a correct complete plan. It SHOULD identify conflicts, affected obligations, and approximate shortfall.

Planner MUST NOT silently make value-laden tradeoffs such as skipping a class to save another deadline unless an explicit user policy authorizes that class of choice.

---

## 41. Planner stability and uncertainty

Small input changes SHOULD NOT unnecessarily rewrite the near-term plan.

Planner SHOULD minimize plan churn. A freeze horizon MAY protect near-term blocks.

Uncertainty SHOULD be represented as uncertainty/ranges rather than fabricated precision.

---

## 42. UI requirements

### Main screen — Now

Should show next recommended action, a few subsequent actions, the next fixed event, latest-safe-departure alert if applicable, and critical conflicts.

### Inbox

Shows unconfirmed candidates, low-confidence imports, suspected duplicates, and source conflicts.

### Plan

Timeline visually distinguishes locked user blocks, planner work, travel, buffers, and fixed events.

### Obligation details

Show separately actual cutoff, target, actionable-from, importance, computed risk, remaining effort, source/provenance, planned work, and milestones.

Do not collapse all times under one generic "Deadline" label.

The UI SHOULD answer: Why now? Why red? Where did this come from? Who set this date? What changed?

---

## 43. Privacy and security boundaries

Treat authentication tokens, calendars, academic records, private source documents/messages, and exact physical addresses/coordinates as sensitive.

LLM integrations SHOULD receive minimum necessary context.

Machine integrations SHOULD use scoped grants/tokens. Long-lived unrestricted credentials MUST NOT be embedded in prompts.

All canonical mutations MUST be authenticated and authorized server-side. An LLM's assertion of permission is never authorization evidence.

---

## 44. Audit

Material changes SHOULD preserve:

```text
who
when
what changed
old value
new value
source
reason/policy
```

Actors MUST distinguish at least:

```text
USER_UI
USER_VIA_LLM
CONNECTOR
PLANNER
SYSTEM
ADMIN
```

Audit/provenance SHOULD survive normal UI archive/deletion where required for reconciliation.

---

## 45. Obligation lifecycle and progress

```text
DRAFT
  ↓
ACTIVE
  ├── COMPLETED
  ├── CANCELLED
  └── ARCHIVED

COMPLETED
  ├── ACTIVE   // reopen
  └── ARCHIVED

CANCELLED
  ├── ACTIVE
  └── ARCHIVED
```

Reopen clears `completed_at`, increments version, and triggers planner/risk/notification recalculation.

Partial progress SHOULD update `remaining_effort`.

---

## 46. Recurrence

Recurring rules MUST be separate from individual occurrences.

```text
RecurringTemplate
↓
Occurrence
```

Editing one occurrence MUST NOT automatically mutate the entire series unless explicitly requested.

---

## 47. Calendar integration

Imported external calendar events are observations mapped to canonical Events.

Removing an event externally SHOULD go through reconciliation rather than unconditional silent hard-delete.

---

## 48. API error model

Errors MUST be structured:

```text
code
message
details
retryable
correlation_id
```

Representative codes:

```text
VERSION_CONFLICT
AMBIGUOUS_CAPTURE
PLAN_INFEASIBLE
PERMISSION_DENIED
SOURCE_CONFLICT
```

API timestamps MUST use unambiguous ISO-8601 with UTC or explicit offset.

---

## 49. Observability

Server operations SHOULD carry request/correlation IDs, actor, operation, latency, result, and error code.

Planner runs SHOULD additionally record plan revision, input hash, feasibility, and recalculation reason.

---

## 50. Product and trust metrics

Do not optimize only for DAU.

Useful metrics include manual captures avoided, manual reschedules avoided, source changes detected, duplicates prevented, critical conflicts caught before failure, obligations completed before cutoff, recommendations accepted, and incorrect imports.

Trust metrics SHOULD include missed-obligation rate, wrong-deadline rate, false duplicate-merge rate, incorrect completion rate, and incorrect infeasibility/risk alert rate.

Trust failures are product-critical.

---

## 51. Feedback

Users MAY report that a recommendation is too early/late, not wanted today, has a wrong effort estimate, or falls in an unavailable time.

Feedback SHOULD adjust preferences/estimates where appropriate but MUST NOT rewrite external hard facts.

LLMs MAY explain server-produced plans and reasons, but SHOULD use authoritative server reason codes rather than invent contradictory explanations.

---

## 52. Rejected target architecture

The following is NOT the target architecture:

```text
Generic Task Manager
+
ChatGPT directly creates canonical tasks
+
Travel is duplicated as tasks
+
One field stores every kind of deadline/time
```

It is rejected because it conflates distinct concepts, duplicates travel state, destroys provenance/reconciliation semantics, cannot preserve target vs external cutoff, gives LLMs excessive ownership, and makes capacity calculation unreliable.

---

## 53. System invariants

The implementation MUST preserve:

```text
server = canonical state
source != extractor != actor
actual_cutoff != target_at
event interval != deadline
scheduled work != completion
travel = transition between locations
return trip is never assumed
LLM never writes canonical DB directly
critical low-confidence facts are not silently committed
external actual_cutoff is never automatically shifted
source changes are auditable
plan cannot be FEASIBLE when constraints prove infeasibility
stale notification cannot survive relevant plan revision
duplicate API retry cannot create duplicate obligation
user target survives external cutoff changes unless explicitly changed
exact private address need not be exposed to an LLM
```

---

## 54. MVP phases

### MVP-0 — Domain core

MUST include Obligation, Task, Event, Project, Milestone, actual_cutoff, target_at, actionable_from, remaining effort, Place, ScheduleBlock, versioning, and audit.

### MVP-1 — Canonical server and manual client

MUST include canonical server, manual capture, lifecycle CRUD, sync, timeline/calendar, fixed events, and basic work blocks.

### MVP-2 — LLM capture

MUST include Capture API, idempotency, CandidateObligation, provenance, field confidence, authority, deduplication, reconciliation, and one LLM/tool adapter.

### MVP-3 — Risk engine

MUST include remaining effort, calendar capacity, slack, risk states, latest safe start, next-actions queue, and explanations.

### MVP-4 — Travel-aware planning

MUST include Place, manual travel overrides, RoutingProvider abstraction, TravelBlock, arrival buffers, latest safe departure, and location-conflict detection.

### MVP-5 — One authoritative connector

Choose one real academic ecosystem and bring it to high trust. Connector MUST support incremental sync, external IDs, provenance, deadline changes, deduplication, and reconciliation.

Build one reliable connector instead of ten shallow connectors.

---

## 55. Explicit non-goals for early MVP

Not required yet:

- plugin marketplace;
- social network;
- gamification platform;
- universal note-taking replacement;
- full email client;
- complex ML optimizer before telemetry exists;
- dozens of LMS connectors;
- unrestricted autonomous agent;
- public marketplace of skills.

---

## 56. Reliability and performance expectations

Typical CRUD SHOULD feel interactive; an engineering target such as p95 below 500 ms is reasonable for server-local work excluding external integrations.

Normal daily-plan recalculation SHOULD finish within seconds for ordinary workloads.

External imports/routing MAY be asynchronous.

Successful server mutations MUST be durable. Notification scheduling, connector checkpoints, and idempotency records MUST survive process restart.

---

## 57. Testing requirements

Time-dependent logic MUST use an injectable clock.

Unit tests MUST cover at least:

- actual cutoff semantics;
- target semantics;
- actionable-from;
- risk transitions;
- capacity/slack;
- latest safe start/departure;
- travel transitions;
- deduplication/idempotency;
- reconciliation;
- optimistic concurrency;
- stale notification invalidation;
- timezones/DST;
- recurrence;
- anti-starvation.

Integration tests MUST cover at least:

- LLM capture -> candidate -> obligation;
- connector import -> obligation;
- external deadline update -> diff/conflict;
- calendar update -> replan;
- event change -> travel recalculation;
- completion -> plan cleanup;
- offline mutation -> sync conflict;
- notification action -> server state.

---

# Acceptance tests

## AT-01 — Explicit LLM capture

Given `User: "Добавь завтра в 18 купить продукты."`, when an LLM calls `capture_obligation`, exactly one obligation is created with `source=USER_UTTERANCE`, `extractor=LLM`, `actor=USER_VIA_LLM`. Retry with the same idempotency key MUST NOT duplicate it.

## AT-02 — Ambiguous inferred deadline

A critical deadline below confidence threshold creates a pending candidate and MUST NOT silently become canonical.

## AT-03 — Authoritative deadline change

When an LMS changes `actual_cutoff` from Sep 22 to Sep 24, the change is recorded, reconciliation runs, source diff is inspectable, and a personal `target_at` remains unchanged.

## AT-04 — Target vs cutoff

Changing `target_at` MUST NOT change `actual_cutoff`.

## AT-05 — External cutoff protection

No planner action, LLM call, snooze, risk policy, or connector default may silently change authoritative `actual_cutoff`.

## AT-06 — Work is not completion

Passing the end of a WORK block MUST NOT automatically complete the Task.

## AT-07 — HSE travel

Given psychologist at 16:00, ten-minute arrival buffer, and HSE travel 45 minutes safe, latest safe departure MUST equal 15:05.

## AT-08 — Dubki travel

With two-hour safe travel and ten-minute arrival buffer, latest safe departure MUST equal 13:50.

## AT-09 — Unknown origin

If origin is unknown and affects feasibility, planner MUST NOT invent origin and MUST expose uncertainty/conditional departures.

## AT-10 — No fake return trip

After an appointment, planner routes to the actual next location rather than automatically returning to the prior origin.

## AT-11 — Impossible transition

If an HSE event ends at 15:40, next appointment starts 16:00, and required travel+buffer is 55 minutes, plan MUST be `INFEASIBLE` with an explanation.

## AT-12 — Capacity proves impossibility

If remaining effort is three hours and available pre-cutoff capacity is two hours, deterministic v1 risk MUST report `IMPOSSIBLE` or stricter equivalent.

## AT-13 — Explainable next actions

Every result from `get_next_actions()` MUST include a reason.

## AT-14 — Duplicate retry

Two capture calls with one idempotency key create one logical obligation.

## AT-15 — Optimistic concurrency

Mutation based on stale entity version MUST fail with a version conflict.

## AT-16 — Stale notification

If relevant plan revision changes before delivery, an invalid notification MUST be suppressed.

## AT-17 — Completion follow-up

Completion check MUST NOT be sent unless the initial reminder was delivered.

## AT-18 — Cancellation

Cancelling an obligation removes future generated work/travel/reminders attributable only to that obligation.

## AT-19 — Reopen

Reopening returns the obligation to risk/planning and clears completed state.

## AT-20 — External disappearance

If connector no longer sees an item, core MUST NOT immediately hard-delete canonical obligation; reconciliation policy runs.

## AT-21 — Private place alias

LLM can use `HOME` without receiving exact home coordinates unless explicitly necessary and authorized.

## AT-22 — Partial progress

Changing remaining effort triggers risk and plan recalculation.

## AT-23 — Locked block

Planner MUST NOT silently move a locked user block; infeasibility becomes a conflict.

## AT-24 — actionable_from

Planner MUST NOT schedule work before `actionable_from`.

## AT-25 — Multiple milestones

One project supports multiple distinct milestones/cutoffs without flattening them into one deadline.

## AT-26 — Recurring exception

Changing one lesson occurrence MUST NOT mutate all future occurrences unless requested.

## AT-27 — Manual display override

If computed risk is green and user sets a red floor, UI distinguishes `computed=GREEN` from `display=RED (manual override)`.

## AT-28 — Anti-starvation

A sufficiently old repeatedly deferred no-cutoff obligation remains discoverable according to configured anti-starvation policy.

## AT-29 — Provenance identity

A PDF parsed by an LLM records `source=PDF`, `extractor=LLM`, not `source=LLM`.

## AT-30 — Restart durability

Restart MUST preserve obligations, versions, audit/provenance, source mappings, idempotency state, connector checkpoints, future notifications, and current plan revision.

---

# Definition of Done — Core v1

Core v1 is complete only when the system has:

- canonical server state;
- Task/Event/Project distinction;
- actual_cutoff / target_at / actionable_from distinction;
- milestones;
- remaining effort;
- Places;
- ScheduleBlocks;
- travel-aware transitions;
- capacity calculation;
- risk engine;
- execution queue;
- Capture API;
- Candidate model;
- source/extractor/actor provenance;
- deduplication;
- reconciliation;
- optimistic concurrency;
- idempotent mutations;
- sync;
- notifications;
- one LLM/tool adapter;
- one real connector;
- audit log;
- automated acceptance tests.

---

# Final product invariant

The user should not have to manually maintain a digital copy of their life every day.

Ideal flow:

```text
system discovers obligation
        ↓
shows provenance
        ↓
asks for confirmation only when necessary
        ↓
updates canonical workload model
        ↓
checks capacity
        ↓
detects deadline/travel risk
        ↓
builds realistic plan
        ↓
shows a few next actions
        ↓
recalculates when reality changes
```

Architectural core:

```text
TRUSTED MODEL OF OBLIGATIONS
+
TEMPORAL / LOCATION CONSTRAINTS
+
RECONCILIATION
+
CAPACITY / RISK
+
EXECUTABLE PLAN
```

LLMs are interfaces/extractors. The planner computes an executable plan. The server remains the owner of canonical truth.
