# ADR 0024 — Recurring work materializes canonical Tasks

## Status

Accepted by implementation branch, 2026-09-27.

## Context

Calendar recurrence already owns recurring fixed-time Events. Reusing that event
template for recurring work would blur attendance/time occupancy with flexible work,
while keeping recurring work as virtual planner rows would create a second execution
and progress model.

## Decision

Recurring work has its own template/occurrence identity, but each in-horizon
occurrence materializes exactly one canonical Task.

- stable recurrence identity is `(template_id, original_recurrence_id)`;
- the generated Task id is deterministic from that identity;
- Task owns effort, remaining effort, lifecycle, execution sessions, planner
  participation and progress;
- the routine template owns recurrence rule, timezone and default task fields;
- moving/editing one occurrence does not rewrite its original recurrence identity;
- skipping an occurrence cancels its Task and marks the occurrence `SKIPPED`;
- reopening restores the same Task; it never creates another occurrence Task;
- stopping a routine prevents future materialization but does not erase history.

The materializer jumps to the planning horizon rather than scanning the full history
of an unbounded series. Civil-time resolution reuses the existing deterministic
recurrence resolver, including DST policy.

Offline occurrence operations carry the stable identity plus the already-materialized
`task_id`. The server verifies that reference before commit; the client can therefore
project skip/reopen/edit onto the same cached Task without inventing a second owner.
Creating a routine offline shows the pending series but does not fabricate future
Tasks before server materialization.

## Consequences

Calendar recurrence and work recurrence remain separate concepts, while all actual
work still flows through one Task/Execution model. Stable identities survive moves,
sync retries and device restarts. Schema v19 adds routine template/occurrence tables.

## Rollback

Schema v19 only adds work-routine tables. After stopping the service and taking a
verified backup, roll back to v18 by dropping `work_routine_occurrences` first,
then `work_routine_templates`, and deleting schema-migration row 19. Materialized
Task obligations are intentionally retained: they are ordinary user work/history
and remain valid in v18 even when the recurrence template is removed.
