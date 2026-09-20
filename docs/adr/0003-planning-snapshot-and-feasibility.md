# ADR 0003 — Immutable PlanningSnapshot and bounded sound feasibility

- Status: Accepted for Pass 2
- Date: 2026-09-20

## Context

The specification requires feasibility to be tri-state: `FEASIBLE` needs a legal witness; `INFEASIBLE` needs a sound proof/complete search for the declared model; heuristic failure, timeout, unsupported semantics, or materially unknown facts must yield `UNKNOWN`. The implementation must also keep feasibility separate from the later planner/risk layer and must not confuse the shorter display horizon with the analysis horizon.

## Decision

Pass 2 introduces an immutable `PlanningSnapshot` built from account-scoped canonical repository state. The snapshot carries the committed `server_revision`, a deterministic SHA-256 `input_hash`, analysis and display horizons, canonical tasks/events/constraints/dependencies/milestones, and a versioned planning policy. Known active hard cutoffs extend the analysis horizon automatically.

The supported exact feasibility model is deliberately small and explicit:

- one-minute aligned times;
- fixed REQUIRED Event occupancy;
- canonical UserTimeConstraints;
- `actionable_from`;
- exact known hard cutoffs with inclusive/exclusive boundary semantics;
- remaining effort;
- splittable/non-splittable work and min/max chunks including final residual chunks;
- hard Task dependencies and hard Event/Milestone successor bounds;
- single-attention work occupancy.

The engine runs cheap fixed contradictions, then a deterministic constructive search, then a finite backtracking exact fallback. Exhausting the complete supported search without a witness can prove `INFEASIBLE`. Exhausting the configured execution/node budget returns `UNKNOWN`. Unsupported sub-minute inputs and intentionally unsupported pinned-work shapes also return `UNKNOWN`.

Before returning `FEASIBLE`, a separate witness verifier re-checks effort totals, chunk rules, hard occupancy, actionable/cutoff boundaries, and dependencies. A solver/verifier disagreement returns `UNKNOWN`; the engine does not trust its own constructive output as proof.

## Strongest alternative considered

A CP-SAT/optimizer dependency could model the same constraints compactly and scale better. It is rejected for Pass 2 because the current bounded model can be searched completely with standard-library code, the repository is intentionally dependency-light, and the specification keeps the solver implementation open. Introducing a heavyweight optimizer now would increase build/runtime complexity without adding a required semantic capability.

## Consequences

- Pass 3 can consume a stable feasibility witness without making heuristic failure synonymous with impossibility.
- `PlanningSnapshot.input_hash` changes with planning-relevant canonical state, horizons, or policy version, so later derived plans can be invalidated safely.
- The exact solver is intentionally bounded rather than production-scale. Large/unsupported cases are `UNKNOWN`, never a fabricated result.
- Travel, soft optimization, risk classification, PlanSnapshot generation, and next-action ordering remain outside this ADR and Pass 2.
