# ADR 0025 — Reflection is user intent plus derived learning evidence

## Status

Accepted by implementation branch, 2026-09-27.

## Context

Execution Sessions finally provide trustworthy actual-work history. That makes daily
reflection and estimation calibration possible, but it also creates a dangerous
shortcut: silently rewriting Task effort or planner policy from observed ratios would
turn descriptive telemetry into autonomous product behavior.

Daily focus has a similar ownership problem. A user saying “these are my priorities
today” is meaningful intent, but treating it as an undeclared hard scheduling
constraint would compete with deadlines, feasibility and explicit pinned time.

## Decision

Reflection & Learning separates three owners.

### 1. Canonical user intent and notes

- `DailyIntent` stores a local date, timezone, optional focus note and up to five
  canonical Task references.
- `DailyReflection` stores the user's summary, wins, blockers and adjustment for a
  local day.
- `WeeklyReview` stores the same explicit reflection fields for a Monday-anchored
  local week.
- Daily focus is presentation/user intent only. It does not pin time or silently
  alter planner priority.

These mutations use the durable offline exactly-once sync boundary and optimistic
versions.

### 2. Derived evidence

Plan-vs-actual statistics are recomputed from source facts:

- actual work = overlap of Execution Segments with the local day; pause time is absent
  because paused intervals are not segments;
- planned day work = WORK blocks in the latest saved PlanSnapshot that existed by the
  end of that local day and overlapped it;
- effort calibration only uses FINISHED sessions explicitly linked to the same
  canonical WORK PlanBlock and Task;
- multiple sessions against one WORK block are aggregated into one calibration sample;
- unmatched/ad-hoc sessions are excluded instead of guessed into a plan;
- a suggestion is withheld until at least three matched blocks exist.

The median actual/planned ratio is descriptive evidence, not a new Task estimate.

### 3. Explicit user decision

A user may explicitly save a bounded calibration multiplier as an estimation hint.
That accepted preference is canonical, versioned state. It is shown beside future
Task effort entry, but it never rewrites existing Tasks, remaining effort or
PlanningPolicy.

## Consequences

The system can learn visibly without becoming a hidden auto-tuner. Historical facts,
user reflections, derived statistics and accepted future hints remain distinguishable.
Offline edits can be projected safely because notes and accepted hints have explicit
owners and versions.

Schema v20 adds daily intent/reflection, weekly review and accepted calibration-hint
tables.

## Rollback

After stopping the service and taking a verified backup, roll back v20 by dropping
`daily_intent_tasks`, `daily_intents`, `daily_reflections`, `weekly_reviews`
and `effort_calibration_preferences`, then deleting schema-migration row 20.
Execution/plan history is untouched because all learning metrics are derived from it.
