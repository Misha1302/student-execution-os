# ADR 0010 — Recurrence identity and notification workflow ownership

- Status: Accepted for Pass 8
- Date: 2026-09-21

## Context

The normative specification requires recurrence to preserve iCalendar-compatible occurrence identity semantics and requires notifications to remain workflow state rather than a second owner of Task/Event truth.

The important boundaries are:

- recurring local time is represented as local civil `DTSTART` plus an IANA timezone;
- occurrence identity is `(template_id, original_recurrence_id)` and does not change when one occurrence is moved;
- a single-occurrence override must not mutate the whole series;
- “this and future” may split a series, but historical occurrences must remain owned by the old series rather than being rewritten;
- notification suppression identity is stable across recomputation;
- notifications bind the domain/plan revisions that justify them and revalidate those revisions immediately before delivery;
- snooze and delivery state are workflow state and do not mutate Task cutoff/target or Event time.

Pass 8 must also feed recurring required Events through the existing PlanningSnapshot → travel → feasibility → planner path. A separate recurrence-only scheduler would create a second planning truth.

## Decision

### Canonical recurrence state

`RecurringTemplate` is canonical local state. It stores:

- account-scoped template identity;
- naive local-civil `dtstart_local`;
- a deliberately small RRULE subset (`DAILY` / `WEEKLY`, optional `INTERVAL`, `COUNT`, `UNTIL`);
- IANA timezone;
- Event category/importance/attendance/location semantics;
- duration and arrival requirement;
- deterministic local-time resolution policy;
- an optional `series_end_before_local` split boundary;
- optimistic version.

The RRULE subset is intentionally fail-closed. Unsupported RRULE components are rejected rather than approximately interpreted.

`OccurrenceOverride` is separately canonical local state keyed by `(account_id, template_id, original_recurrence_id)`. `CANCEL` carries no replacement fields. `MODIFY` retains the original recurrence identity and carries replacement local start/duration.

### DST/local-civil policy

Pass 8 uses `EARLIER_FOLD_SHIFT_FORWARD`:

- an ambiguous local time chooses the earlier fold deterministically;
- a nonexistent local time shifts forward to the first valid local minute;
- the occurrence identity remains anchored to the original local-civil recurrence position, not the shifted instant.

This is an explicit implementation policy, not a claim that it is the only acceptable product policy.

### Series split

“This and future” is implemented by terminating the old template immediately before the selected original recurrence identity and inserting a successor template. Historical rows and historical original recurrence identities are not rewritten.

COUNT-limited series require an explicit successor RRULE because automatically redistributing the old COUNT across the split would silently invent semantics.

### Planning integration

`SQLitePlanningStateSource` expands recurring templates into finite-horizon derived Event projections. Those Events enter the same immutable PlanningSnapshot as static Events and therefore participate in the existing travel projection and feasibility/planner contracts.

Recurring occurrence projection is derived state. The canonical writable owners remain `RecurringTemplate` and `OccurrenceOverride`; generated occurrence Events are not independently mutable truth.

### Notification workflow

`Notification` is workflow state in schema v7. It stores:

- stable suppression key and stable deterministic notification id;
- kind/entity reference;
- current domain revision and optional plan id/revision justification;
- scheduled, cooldown, snooze and delivery timestamps;
- delivery state/attempt count/error;
- optimistic version;
- optional initial-notification link for completion follow-up.

Notification mutations deliberately do not advance canonical `server_revision`.

Before delivery, the repository revalidates the captured domain revision and, when present, current plan identity/revision. A stale trigger is suppressed before calling the external sender.

A completion follow-up may be queued in advance but cannot be delivered until its initial notification is actually `DELIVERED`.

Re-scheduling the same suppression key updates the revision binding/timing of the same logical notification rather than inserting another row. A user snooze remains a snooze across that recomputation.

Quiet hours are evaluated in an IANA timezone and defer delivery to the deterministic end of the quiet interval.

### Bounded risk evaluation

Pass 8 exposed a pre-existing performance flaw: `RiskEngine.timeout_seconds` was effectively multiplied by every scenario/latest-safe-start sub-search. Recurring Events made this visible through normal `/tasks` and browser reads.

`RiskEngine.timeout_seconds` now bounds the whole risk evaluation. Each feasibility sub-search receives only the remaining wall-clock budget. Exhaustion fails closed to `UNKNOWN` with `RISK_EVALUATION_BUDGET_EXHAUSTED`; it never fabricates `FEASIBLE`, `INFEASIBLE`, or a latest-safe-start value.

## Alternatives considered

### Materialize every recurrence occurrence as a canonical Event

Rejected because it creates duplicate writable ownership between the recurrence rule and generated Events and makes series edits/history ambiguous.

### Store recurrence in UTC instants only

Rejected because it cannot preserve intended local civil time across DST transitions.

### Treat notification schedule/snooze as Task/Event fields

Rejected because delivery workflow would become a second owner of canonical obligation truth and snooze could accidentally mutate deadlines.

### Give every internal feasibility call a fresh full timeout

Rejected because a nominal two-second risk budget could expand to tens of seconds or minutes. A single global budget preserves fail-closed semantics and predictable execution.

## Verification contract

Pass 8 must retain all previous tests and cover at least:

- AT-50 — moved occurrence keeps original recurrence identity;
- AT-51 — “this and future” split preserves history;
- AT-52 — local civil recurrence across DST;
- AT-53 — stale notification suppressed before delivery;
- AT-54 — completion follow-up requires successful initial delivery;
- AT-55 — snooze changes workflow timing only;
- AT-68 — repeated recomputation does not create duplicate notifications;
- recurrence projections entering the ordinary planning/travel input path;
- v6 → v7 migration preservation;
- risk total-budget exhaustion returning `UNKNOWN`;
- API/UI presentation of canonical recurrence rules vs derived occurrences and notification workflow state.

## Known limits carried to Pass 9

- No external notification channel/provider is configured.
- Sender invocation is not yet protected by a durable delivery lease/outbox, so crash/concurrent-delivery exactly-once hardening remains Pass 9 reliability work.
- The recurrence RRULE subset is intentionally limited; unsupported RFC 5545 features fail closed.
- Very distant moved overrides on an unbounded series can require long recurrence expansion scans; Pass 9 should add explicit expansion/work budgets if production-scale recurrence requires it.
