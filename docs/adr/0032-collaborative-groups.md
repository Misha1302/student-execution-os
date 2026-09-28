# ADR 0032 — Collaborative academic groups on the SOURCE/USER model (schema v27)

**Status:** Accepted for R8 (supersedes the unmerged `feature/collaborative-groups`
branch, schema v18 / ADR 0021 draft)

## Context

A study group shares academic reality (the class schedule, exams); each student decides
what to do about it. The earlier branch modelled this with its own shared-event tables,
per-user overlay tables and a separate agenda projection, built on schema v17. Since
then R3 made "imported reality vs personal intent" a first-class canonical model:
`SourceApplier` is the only SOURCE writer, the command boundary owns the USER layer,
external identities are stable, and personal changes survive source refreshes. Merging
the old branch would have created a second, parallel model of the same thing.

## Decision

- **The group is a source.** It owns shared schedule items (recurring classes as
  `SourceSeries`, one-off events such as exams as `SourceEvent`, with the group's own
  occurrence changes). Each ACTIVE member receives them through `SourceApplier` into
  their own account, under a source system unique to member+group. Classes therefore
  appear in Calendar, Today, the planner and reminders exactly like imported classes.
- **Personal stays personal.** A member's move/cancel/room note of a class, tasks,
  notes, reminders, progress and AI state live in their account's USER layer and
  canonical tables; group code never reads or writes them, the group API never returns
  them, and a group change never overwrites them (R3 precedence).
- **Roles:** OWNER (all, incl. roles/removals), STAROSTA (publish, decide proposals,
  invite, remove MEMBERs), MEMBER (read, propose). At least one owner always exists; if
  the last owner's account is deleted, the longest-standing starosta (else member)
  inherits ownership.
- **Proposals:** members propose UPSERT/REMOVE of an item; nothing is published until a
  starosta/owner approves; decisions are idempotent and final; proposers may withdraw.
  Members see only their own proposals.
- **Membership:** invitation codes (hashed, expiring, use-limited, revocable); joining
  materializes the schedule; leaving or removal retracts it (an empty complete snapshot)
  while the member's own data stays; rejoining restores the same identities and any
  personal overlay. Removed members cannot rejoin with a code.
- **Consistency:** every write is idempotent by a client id; schedule edits carry
  `expected_revision`, checked by a conditional update inside the write transaction;
  fan-out to all members happens in that same transaction.
- **Lifecycle:** group tables are shared (not exported with one account); memberships
  cascade on account deletion; v27 rollback fails closed while any group exists.

## Consequences

One model for "reality vs intent" whether it comes from a university feed or a
starosta. Not ported from the old branch: announcements, shared obligations/deadline
tasks, per-member attendance/criticality settings, diff notices and external calendar
binding — those are separate product decisions. Group management is online-only (not
queued offline); personal edits of group classes remain offline-capable via
`/api/v1/sync`. Publishing cost grows with members × items (caps: 300 members, 400
items).
