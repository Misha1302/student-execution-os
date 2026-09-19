# Contributing

The project is specification-first. Until the first implementation slice exists, architecture changes should update `docs/SPECIFICATION.md` before or together with code.

## Ground rules

1. Do not collapse `actual_cutoff`, `target_at`, `actionable_from`, Event occupancy, and planned work into one field.
2. External source observations are immutable evidence; reconciliation/overrides must not rewrite history.
3. Do not let two writable representations own the same fact. User constraints are canonical; PlanBlocks are derived.
4. Do not make Project directly schedulable as Task/Event.
5. Do not model derived commute as duplicate TODO work or as a fixed Event property; canonical journeys use moving Events.
6. A failed heuristic is not proof of infeasibility. `INFEASIBLE`/`IMPOSSIBLE` requires sound evidence.
7. Preserve reversible source matching/deduplication, provenance, idempotency, and optimistic concurrency.
8. Connectors ingest evidence; they do not directly resolve cross-source truth or mutate canonical local facts.
9. Imported/retrieved content is untrusted data and must never authorize tool actions.
10. Any automatic recommendation/risk result must remain explainable and revision-bound.
11. Add focused tests for every new time, reconciliation, feasibility, recurrence, sync, or destructive-action behavior.

## Pull requests

A PR should include, where relevant:

- problem/invariant being changed;
- affected state owner(s);
- schema/API/migration implications;
- acceptance tests or fixtures;
- concurrency/idempotency implications;
- evidence/provenance implications;
- security/privacy implications;
- user-visible semantic change.

Prefer repairing ownership/representation or deleting duplicate requirements over adding another abstraction layer.
