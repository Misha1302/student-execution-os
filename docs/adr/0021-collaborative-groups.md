# ADR 0021 — Collaborative academic groups (schema v18)

## Status

Accepted, 2026-09-26. Schema v18 (migration `018_collaborative_groups.sql`).

## Context

Students share one reality — a group's classes, control works, deadlines and notices —
but each of them decides differently what to do about it. The product needs groups
without a second sync, reminder, undo or recurrence engine, without a second agenda and
without anything specific to one university.

Invariant: **the group tells the user WHAT is happening; the personal OS decides WHAT
THE USER should do about it.**

## Decision

### Domain (`groups/model.py`)

- `Group` (typed `GroupSettings` / `GroupJoinPolicy` columns, status
  ACTIVE → ARCHIVED ⇄ ACTIVE → DELETED) and `Membership` (role OWNER / ADMIN /
  SCHEDULER / MEMBER; status PENDING / ACTIVE / LEFT / REMOVED / BLOCKED).
- Shared facts: `SharedEvent` (an assessment is an event `kind` — QUIZ, CONTROL_WORK,
  EXAM… — with REQUIRED + CRITICAL defaults, not a separate entity), `SharedObligation`
  (deadline), `SharedAnnouncement` (informational only: never an event or a task).
  States PUBLISHED → CANCELLED (events, obligations) or RETRACTED (announcements); a
  terminal entity is kept for its audit trail and cannot be republished by a patch.
- `GroupProposal` is its own entity (PENDING → APPROVED / REJECTED / WITHDRAWN) with a
  tagged-union payload (`kind` = the proposed entity kind). Shared entities have no
  PROPOSED status. Approve creates exactly one entity with a deterministic id
  `pub-<hash(proposal_id)>`; a second approve is `NOOP ALREADY_APPROVED`;
  EDIT_AND_APPROVE keeps the original and the approved payload apart.
- `ExternalEventBinding` ties a shared event to one occurrence of an imported calendar
  event, source-agnostically (`provider:scope` or `kind:source_system_id`, series uid,
  occurrence key). Official fields (time, place) stay owned by the source: a group
  change to them is `REJECTED EXTERNAL_OWNED`; `reconcile_external_bindings` (run by the
  existing worker tick) follows official changes and keeps identity and overlays.
- Personal overlays, readable only by their owner: `UserSharedEventState` (attendance
  REQUIRED/PREFERRED/OPTIONAL/SKIP, personal criticality, hidden, linked preparation
  task, last seen version), `UserSharedObligationState`, `UserAnnouncementState`,
  `UserGroupPreferences` (subscribed kinds, `notification_behavior`, mute).

### Authorization

Capabilities (`VIEW_SHARED`, `CREATE_PROPOSAL`, `PUBLISH_SHARED`, `MODERATE_PROPOSALS`,
`BIND_EXTERNAL`, `MANAGE_MEMBERS`, `MANAGE_INVITES`, `MANAGE_SETTINGS`, `MANAGE_ROLES`, …) come from `ROLE_CAPABILITIES` plus group settings
(`allow_members_to_publish`). Every check is `capability in caps`, on the server; roles
are never compared numerically (a unit test scans for it). Non-members and entities
outside the group named in the path answer `NOT_FOUND` (IDOR). Invite tokens are stored
hashed and shown once; codes are case-insensitive; join attempts (including unknown
codes) are rate-limited from `client_operations`; publish/propose/invite/group-create
have hourly/daily limits (HTTP 429, not recorded as an operation).

### One mutation path

All group operations are `Commands` handlers executed by `SyncService.apply`: one
transaction, `client_operations` replay by `mutation_id` (the HTTP `Idempotency-Key`),
outcomes APPLIED / NOOP / CONFLICT / REJECTED. Shared entities carry `version`; an edit
requires `expected_version` (body or `If-Match`) and a stale one is
`CONFLICT` with `current_version` — no silent last-writer-wins. Every group change
writes `group_audit` (actor, action, before/after diff). `Outcome.transient` carries a
one-time invite secret to the caller without storing it in the replay record.

The offline queue (`/api/v1/sync`, channel `offline-queue`) accepts only the personal
operations (`shared_event_state.update`, `shared_obligation_state.update`,
`announcement_state.update`, `group_preferences.update`, and `task.create` with
`prepares`); group-wide operations answer `REJECTED ONLINE_ONLY`, and the client sends
them online with one mutation id per form.

### One agenda, one planner, one reminder engine

`PersonalProjection` (`/api/v1/me/shared`) is the only source for the member's view of
group facts. It feeds:

- the «Дела» agenda (`web/commitments.py` ⇄ `js/agenda.js` gained shared kinds; an
  annotation of the member's own imported event is attached to that EVENT row, never a
  second row);
- the planner: effective REQUIRED/PREFERRED events become derived UNAVAILABLE
  constraints `shared:<id>` next to the off-hours ones (`derived_availability`);
- the reminder engine: `SHARED_EVENT` / `SHARED_OBLIGATION` facts use the existing
  ladder (`LADDER_KINDS`); the critical ladder is opt-in (`CHANGES_AND_CRITICAL`), is
  skipped for SKIP attendance and when a linked open preparation task already carries it.

Preparation is an ordinary private task (`task.create` with `prepares {kind,id}`):
default cutoff = event start / deadline, importance from effective criticality. Nobody
completes a shared fact for the group; nothing personal is created without consent.

### Fan-out on change

Reschedule/cancel (`groups/fanout.py`) moves a linked task's cutoff when it still equals
the old start (otherwise flags it), moves `reminder_states.remind_at`, moves or cancels
the standalone ALARM reminder, and enqueues notification *diffs* («Контрольная
перенесена: чт 12:10 → пт 10:30») through `reminder_messages` with per-recipient urgency
(personal criticality wins) and `group:` dedupe keys. Retract removes undelivered notices.

### Assistant

Ten typed `AgentCommand` values (`CREATE/UPDATE/CANCEL_SHARED_EVENT`,
`CREATE_SHARED_OBLIGATION`, `CREATE/APPROVE/REJECT_GROUP_PROPOSAL`,
`SET_PERSONAL_EVENT/OBLIGATION_PREFERENCES`, `CREATE_PREPARATION_TASK`) go through the existing interpret → preview →
confirm → apply pipeline with mutation id `assistant:<action id>`. Group-wide commands
always require explicit confirmation; the model cannot raise a member's capabilities.

### Account deletion

Before the tombstone, `_release_group_ties` hands ownership to the oldest ADMIN, then
SCHEDULER, then MEMBER (or marks the group DELETED), deletes the account's overlays and
memberships, and pseudonymises authorship (`deleted-account`) in shared facts and audit.

## Rollback

```sql
DROP TABLE user_announcement_states; DROP TABLE user_shared_obligation_states;
DROP TABLE user_shared_event_states; DROP TABLE user_group_preferences; DROP TABLE group_audit;
DROP TABLE external_event_bindings; DROP TABLE group_proposals; DROP TABLE shared_announcements;
DROP TABLE shared_obligations; DROP TABLE shared_events; DROP TABLE group_invites;
DROP TABLE group_memberships; DROP TABLE groups; DELETE FROM schema_migrations WHERE version=18;
```

Group notices already in `reminder_messages` and preparation tasks stay valid personal
data. `tests/integration/test_v14_migration.py` exercises this script.

## Consequences / known limits

- Group-wide changes need the network; personal decisions work offline.
- External annotation binds one occurrence, not a whole series.
- The official snapshot is refreshed by the worker tick from the binder's local copy.
- Group commands are understood through the AI typed actions; the deterministic local
  RU/EN grammar does not parse them yet.
- A client sees new group items after its cached `/me/shared` is refreshed.
