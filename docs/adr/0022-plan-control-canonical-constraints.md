# ADR 0022 — Plan control is canonical constraints, not mutable PlanBlocks

## Status

Accepted by implementation branch, 2026-09-27.

## Context

The planner owns derived `PlanBlock` rows. Users still need direct control over a
plan: pin a task to a time, move it, or reserve time the planner must avoid. Writing
those gestures back into PlanBlocks would create a second source of truth and make a
replan indistinguishable from a user commitment.

## Decision

User plan control is represented only through existing canonical
`UserTimeConstraint` entities:

- pin/move work -> `PINNED_WORK` referencing the Task;
- avoid time -> `UNAVAILABLE`;
- personal fixed blocks remain `FIXED_PERSONAL_BLOCK`;
- drag is only an interaction shortcut that creates/updates the same constraints;
- PlanBlocks remain immutable planner output.

Every interactive mutation first supports a rollback-only server preview. The preview
applies the exact canonical command inside a transaction, runs the real planner on
the hypothetical snapshot, serializes before/after, then rolls the transaction back.
No preview revision, audit row, or constraint survives.

Actual apply goes through the normal durable offline operation queue
(`constraint.create/update/delete`). If the device is offline, the intended
canonical constraint is projected immediately, while stale derived WORK blocks are
hidden until the server can replan. The client never fabricates a replacement plan.

## Consequences

- one ownership model survives online, offline, drag, and explicit forms;
- planner feasibility remains the authority for the resulting schedule;
- offline UI may temporarily show fewer derived blocks rather than a false schedule;
- existing constraint optimistic concurrency provides multi-device conflict handling;
- no schema migration is required for this release.
