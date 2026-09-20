# ADR 0004 — Derived PlanSnapshot, deterministic risk, and first execution slice

Status: Accepted for Pass 3
Date: 2026-09-20

## Context

Pass 2 can answer hard feasibility soundly, but feasibility is not a user plan. The first vertical slice also needs immutable derived plan history, deterministic risk, explainable next actions, and a real capture-to-plan interaction without creating a second writable source of truth.

## Decision

Pass 3 keeps three owners separate.

1. FeasibilityEngine decides only whether a legal hard-constraint witness exists.
2. Planner turns a verified witness into immutable derived PlanSnapshot and PlanBlock projections. It may choose deterministic soft ordering, but it cannot turn planner failure into hard infeasibility.
3. RiskEngine evaluates deterministic LOW / EXPECTED / HIGH effort scenarios and applies the normative precedence rules. Computational uncertainty remains UNKNOWN.

Task effort uncertainty is represented explicitly by optional low/high bounds around the existing expected effort. No invented percentages or confidence values are used.

latest_safe_start is obtained by repeated sound feasibility checks while monotonically tightening the target Task actionable lower bound. It is never computed as cutoff minus duration.

Derived plans are persisted in append-only history tables. current_plans is only a convenience pointer: a plan is authoritative/current only when its stored input_hash matches the freshly built PlanningSnapshot. Canonical mutations therefore make the old plan stale without mutating or deleting history.

WORK blocks remain derived. A pinned WORK block references a canonical UserTimeConstraint. Pinning never promotes a PlanBlock into canonical truth.

The deterministic display mapping is:
- NOT_APPLICABLE -> TRANSPARENT
- SAFE -> GREEN
- START_SOON or UNKNOWN -> YELLOW
- AT_RISK or CRITICAL -> RED
- IMPOSSIBLE or OVERDUE -> BURNING

Importance, computed risk, and colour remain separate fields.

## Planner soft order

Hard legality always wins. Among ready Tasks the deterministic constructive ordering prefers:
1. Tasks with target_at, ordered by target;
2. otherwise Tasks with a known hard cutoff, ordered by cutoff;
3. otherwise older Tasks;
4. higher importance;
5. stable Task id.

This is intentionally a small deterministic policy, not a claim of global soft-objective optimality. Identical snapshot plus policy produces equivalent plan decisions.

## Persistence

Schema v2 adds optional effort-bound columns and immutable derived plan history. Saving a derived PlanSnapshot does not advance canonical server_revision.

## Strongest alternative considered

A combined optimizer could jointly solve feasibility, soft planning, risk margins, and churn. It is deferred because it would blur proof ownership and add solver infrastructure before the product objective is validated. The exact feasibility core remains the correctness oracle; planning stays a replaceable projection layer.

## Deferred

Travel/location routing, recurrence, evidence/reconciliation, optional/preferred Event omission policy, notifications, and production-scale optimization remain later passes.
