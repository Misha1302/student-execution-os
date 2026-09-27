# ADR 0024 — Recurring work materializes canonical Tasks

## Status

Accepted by implementation branch, 2026-09-27. Schema v19.

## Context

Calendar recurrence already owns fixed-time recurring Events. Flexible repeated work
has different semantics: "practice algorithms for 45 minutes every day" must consume
planner capacity but must not pretend that 18:00 is a fixed calendar event.

## Decision

Recurring work has its own template/occurrence identity while each occurrence
materializes one ordinary canonical Task:

- identity is `(template_id, original_recurrence_id)`;
- the generated Task owns effort, remaining work, execution, completion and planner
  participation;
- the template owns recurrence rule, timezone, default effort and series lifecycle;
- skip/reopen operate on one stable occurrence and its Task;
- edit-one may change title, effort or local target without changing occurrence identity;
- "this and future" splits the series at the selected original recurrence id;
- a split removes only untouched generated future Tasks and then materializes the
  successor series. Started, edited, project-linked or attachment-bearing future
  occurrences cause an explicit conflict instead of history rewrite;
- COUNT-limited series carry only their remaining count into the successor.

Local civil recurrence time uses the existing deterministic DST resolution policy.
The planning snapshot materializes the upcoming horizon before it reads Tasks, so no
second planner entity exists.

## Offline semantics

Template and occurrence mutations use the same durable exactly-once operation queue.
Skip/reopen/edit are projected onto the cached canonical Task immediately. A pending
series split is shown as pending intent; the client does not fabricate successor Tasks
until the server validates and materializes them.

## Rollback

After a verified backup, schema v19 can roll back to v18 with:

```sql
DROP TABLE work_routine_occurrences;
DROP TABLE work_routine_templates;
DELETE FROM schema_migrations WHERE version=19;
```

Generated Tasks are canonical user data and are not deleted by schema rollback. A
production rollback procedure must therefore restore the pre-v19 backup if it needs
an exact v18 data image.

## Consequences

Recurring events and recurring work remain distinct. Planner/execution/progress code
continues to reason only about ordinary Tasks and Events, while recurrence identity
remains stable and auditable.
