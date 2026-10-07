# ADR 0036 — Checklists inside Tasks, quotas in the day budget, progress from real data (schema v33)

**Status:** Accepted

## Context

The roadmap asked for subtasks/checklists, "N units per day" goals and milestone-based
project progress with history. Effort must not be counted twice, quantities must not be
turned into invented time, and project progress must not become a second truth.

## Decision

1. **A subtask is a step of one Task** (`task_subtasks`), never a hidden top-level Task.
   The planner never sees subtasks; the Task's remaining effort stays the only planning
   effort. A step's own effort is a breakdown hint shown next to the Task's estimate
   (with a note when they differ); completing a step does not change the Task's
   remaining effort or complete the Task. Order is a fractional `position` so offline
   devices insert and move steps without renumbering. Field edits are last-writer-wins;
   complete/reopen are idempotent intents; deletes are tombstoned; the Task's deletion
   removes its checklist. Operations may carry a validated `task_id` so lists update
   offline.
2. **Daily quotas are check-ins (ADR 0034)**, not boolean Tasks: quantity is the truth
   (`quantity_done` of `target_quantity`). With a user-given pace
   (`unit_effort_seconds`) the remaining quantity becomes minutes that Today reports and
   subtracts from the day's free time (`safe_reserve_after_quotas_minutes`); without a pace
   no time is invented and the quota is counted as "without a time estimate". Quotas are
   not placed as plan blocks.
3. **Project progress is derived** from members: EFFORT basis from the Tasks' canonical
   remaining effort; TASK_COUNT basis (no estimates) counts finished Tasks whole and open
   ones by their checklist share. Milestones report done / total / overdue and the next
   one. History is reconstructed from the dates work was actually finished (Task
   completion and step times) against today's scope — no interpolated points.

## Consequences

* No double counting: checklist effort is never added to the plan.
* Not done: quotas as scheduled blocks; a per-day stored progress series (history is
  reconstructed and changes if scope changes); subtasks in the Assistant.
