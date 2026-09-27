# ADR 0023 — Projects are containers over existing obligations

## Status

Accepted by implementation branch, 2026-09-27.

## Context

The canonical schema already contains Project, project_members and Milestone. Turning
Project into another schedulable obligation or storing a manually edited progress
percentage would duplicate Task ownership and planner semantics.

## Decision

Project remains a container aggregate:

- project membership points to canonical Task/Event obligations;
- checklist creation creates a real Task and then links it to the Project atomically;
- milestones are canonical project markers, not replacement task deadlines;
- Project itself never consumes plan capacity;
- project progress is derived from member Task effort when known, otherwise member
  completion count;
- project risk is derived from the strongest current member Task risk and an overdue
  active project milestone;
- project lifecycle does not silently complete/cancel child obligations.

Project, membership and milestone mutations use the same offline exactly-once sync
boundary as Tasks. The client projects queued changes onto cached Project read models.

## Consequences

There is one owner for task effort/progress/risk and one owner for project grouping.
A Project can be completed while child Task history remains intact, and a completed
child Task does not silently close the Project.
