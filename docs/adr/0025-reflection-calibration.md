# ADR 0025 — Reflection is derived; calibration is explicit and planning-only

## Status

Accepted by implementation branch, 2026-09-27. Schema v20.

## Context

Execution sessions now record actual work, while plan snapshots preserve what the
planner proposed. That makes useful review possible without introducing a second
manual progress model. The product also needs a way to learn from repeated estimate
error without silently rewriting user estimates or turning a review screen into an
opaque productivity score.

## Decision

### Daily Intent

A daily intent stores only:

- local date;
- up to three open Task ids;
- an optional note;
- start/close timestamps.

The selected tasks are a **soft planner signal**. Hard timing, feasibility,
dependencies, risk ordering and canonical user constraints remain stronger. Closing
the day removes the signal from future replans for that local date.

### Reflection

Weekly/daily review metrics are derived on read from canonical history:

- planned work = the first saved plan generated during each local day (falling back
  to the plan already in force at midnight only when no plan was generated that day);
- actual work = execution-segment time clipped to the review interval;
- completed work = canonical Task completion timestamps;
- schedule churn = Task placement changes between saved plan snapshots;
- carry-over = still-open Tasks that appeared in work blocks during the interval;
- estimate bias = actual execution time divided by the estimate captured at the
  first execution session for completed Tasks.

No productivity score, streak score, or manually edited review percentage is stored.

### Calibration

After at least five completed observations in one Task category, the review may show
a median-based safety-multiplier suggestion. Suggestions never apply automatically.

If the user explicitly accepts a multiplier:

- it is stored as a category calibration preference;
- the planner/risk projection uses the multiplier for remaining effort;
- the canonical Task estimate and remaining-effort fields are not mutated;
- the active multiplier is included in the planning snapshot hash and plan
  explanations;
- the user can disable it or suppress future suggestions.

This keeps "what I estimated" separate from "how conservatively the planner should
reserve time".

## Offline semantics

Daily intent and calibration preferences use the same durable exactly-once sync
queue as other user-owned state. The device projects those canonical choices
immediately. Because either choice changes planner inputs, stale derived WORK blocks
and next actions are hidden until the server confirms the mutation and replans.

## Rollback

Schema v20 can roll back to v19 with:

```sql
DROP TABLE calibration_preferences;
DROP TABLE daily_intents;
DELETE FROM schema_migrations WHERE version=20;
```

The tables contain only v20 preferences; canonical Task, execution and plan history
are untouched.

## Consequences

Learning remains explainable and reversible. Historical facts are not overwritten by
a model-generated estimate, and planner adaptation requires explicit user consent.
