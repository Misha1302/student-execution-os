# Student Execution OS — Implementation Handoff

> Checkpoint only. Re-read current repository, specification, PR/branch state, exact commit refs and CI before treating this file as current truth.

## Identity

- Repository: `Misha1302/student-execution-os`
- Pass 8 baseline: merged UI/main `a76a2256ed1768fc648123230d184b583a237862`
- Normative specification: v2.1
- Pass 8 branch: `impl/pass-8-recurrence-notifications`
- Schema: v7
- Package candidate: `0.5.0.dev1`
- Date: 2026-09-21

## Completed before Pass 8

- [x] Pass 0 — baseline / stack / skeleton / CI
- [x] Pass 1 — canonical local domain / persistence / concurrency
- [x] Pass 2 — immutable PlanningSnapshot / sound tri-state feasibility
- [x] Pass 3 — planner / risk / PlanSnapshot / next actions
- [x] Pass 4 — evidence / reconciliation / provenance
- [x] Pass 5 — Google Calendar evidence connector
- [x] Pass 6 — LLM extraction / authenticated action boundary
- [x] Pass 7 — travel-aware planning
- [x] Product UI stage — PR #10 merged at `a76a2256ed1768fc648123230d184b583a237862`, post-merge CI green

## Pass 8 candidate

Implemented recurrence and notification workflow ownership without creating another planning or Task/Event truth.

### Recurrence

- canonical account-scoped `RecurringTemplate`;
- local-civil DTSTART plus IANA timezone;
- fail-closed RRULE subset: DAILY / WEEKLY with INTERVAL / COUNT / UNTIL;
- stable occurrence identity `(template_id, original_recurrence_id)`;
- `CANCEL` / `MODIFY` occurrence overrides;
- “this and future” split into old + successor template without rewriting history;
- deterministic ambiguous/nonexistent local-time policy;
- recurring Event projections enter the ordinary PlanningSnapshot, travel and feasibility path;
- Calendar UI distinguishes `CANONICAL RULE` from `DERIVED OCCURRENCE`.

### Notifications

- notification workflow state persisted separately from canonical Task/Event truth;
- stable suppression key / deterministic id;
- captured domain revision plus optional plan id/revision;
- stale revision suppression immediately before sender invocation;
- completion follow-up gated on successful initial delivery;
- snooze with optimistic versioning and no canonical server revision mutation;
- quiet hours in an IANA timezone;
- deterministic grouping/cooldown/recomputation behavior;
- Settings/UI surface exposes notification state and safe snooze semantics.

### Risk execution budget repair

A pre-existing runtime problem became visible once recurrence increased planning complexity: the two-second `RiskEngine` budget was being granted independently to every scenario and every latest-safe-start sub-search. One read could therefore multiply the configured budget many times.

The candidate now uses one wall-clock deadline for the whole risk evaluation. Sub-searches receive only the remaining budget. Exhaustion returns `UNKNOWN` / `RISK_EVALUATION_BUDGET_EXHAUSTED`, preserving the specification’s fail-closed exact-feasibility semantics.

### Browser harness repair

The Chromium suite now owns one Playwright driver/browser per test class and closes per-test pages. This removes repeated driver start/stop churn while preserving real Chromium coverage.

## Acceptance coverage

Pass 8 adds executable coverage for:

- AT-50 — moved occurrence keeps its original recurrence identity;
- AT-51 — “this and future” preserves historical occurrences;
- AT-52 — intended local civil time survives DST transition under the declared policy;
- AT-53 — obsolete revision-bound notification is suppressed before channel delivery;
- AT-54 — completion follow-up is blocked until the initial reminder was delivered;
- AT-55 — snooze mutates notification workflow timing only;
- AT-68 — repeated recomputation rebinds one logical notification rather than duplicating it;
- recurrence entering the same planning/travel input path;
- quiet-hours deferral;
- v6 → v7 migration preservation;
- risk-engine total budget exhaustion failing closed to UNKNOWN;
- recurrence/notification API and browser presentation.

## Local verification checkpoint

The current Pass 8 candidate has a terminal `make verify` result with exit code **0** on 2026-09-21:

- core unit/integration/acceptance suite: **148/148 PASS**;
- web/API acceptance suite: **12/12 PASS**;
- real Chromium UI suite: **3/3 PASS**;
- all 9 CLI smoke surfaces, including `recurrence-notification-smoke`: **PASS**;
- web-host CLI smoke: **PASS**;
- Python compile + frontend JS syntax check: **PASS**.

This remains local candidate evidence until terminal GitHub branch HEAD, exact-head hosted CI, PR mergeability and post-merge CI are freshly observed.

## Known limits / Pass 9 targets

- Production authentication/TLS/secret-management/deployment remains outside this implementation.
- Google Calendar still lacks production OAuth consent/refresh lifecycle.
- No live routing/maps provider exists.
- No live LLM provider exists.
- No external notification channel is configured.
- Notification sender execution still needs Pass 9 durable delivery lease/outbox/crash-retry hardening for stronger duplicate-delivery guarantees.
- Recurrence supports a deliberately limited RRULE subset; unsupported components fail closed.
- Unbounded recurrence expansion should receive explicit production work budgets if usage requires very distant moved overrides.
- Restart/backup/restore, export isolation, observability and final production-boundary hardening remain Pass 9/10 work.

## Next pass after safe Pass 8 merge

Pass 9 — reliability / security / hardening. Re-read the current specification, current `main`, Pass 8 merge commit and exact post-merge CI before mutation. Build a fresh gap ledger rather than assuming this handoff is current.
