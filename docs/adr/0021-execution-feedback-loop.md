# ADR 0021 — Actual execution sessions and feedback loop (schema v18)

## Status

Accepted by implementation branch, 2026-09-27. Schema v18.

## Context

The planner already distinguishes canonical obligations from derived PlanBlocks, but
"Start" only stored a timestamp on the Task. That could not answer how much time the
user actually worked, could not exclude pauses, and encouraged treating elapsed time
as progress. It also made repeated work on the same Task indistinguishable.

## Decision

Actual work is a separate canonical history:

- `ExecutionSession` identifies one period of working on one Task.
- `ExecutionSegment` records active intervals; pauses close a segment and resume
  opens another.
- At most one ACTIVE/PAUSED session exists per account by default.
- `planning_snapshot_id` and `source_plan_block_id` are advisory provenance only.
  PlanBlocks stay derived and immutable.
- Actual work never decrements `remaining_effort_minutes` by itself. On finish the
  user chooses COMPLETE, UPDATE_REMAINING, KEEP_REMAINING, or CONTINUE_LATER.
- Session transitions use the existing offline operation queue and exactly-once
  `client_operations` boundary.
- A direct Task completion closes an active session for that Task so actual execution
  cannot remain running after the obligation is done.
- Execution history is account-scoped, exported with account data, and deleted with
  the account.

The existing Task `started_at` / `last_progress_at` fields remain compatibility
signals for reminders and older clients. They are not the source of actual duration.

## Multi-device semantics

The database has a partial unique index that permits only one non-terminal session
per account. A concurrent start from another device is an explicit sync CONFLICT, not
last-write-wins. The client keeps the refused operation visible through the existing
sync problem sheet.

## Offline semantics

The device projects queued execution operations onto Today, Tasks and
`/api/v1/execution/active`. Start/pause/resume/finish therefore appear immediately
and survive restart once the core read models have been cached. Server replay remains
exactly-once by operation id.

## Consequences

- Planned minutes, actual work and remaining effort are three distinct values.
- Pause time is not counted as work.
- Historical sessions remain inspectable after replanning.
- Planner recalculation is driven only by confirmed Task-state changes, not by the
  mere passage of timer time.
- Future calibration can consume execution history without rewriting user estimates.
