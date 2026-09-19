# Contributing

The project is currently specification-first. Before application code is accepted, changes should preserve the domain invariants defined in `docs/SPECIFICATION.md`.

## Ground rules

1. Do not collapse `actual_cutoff`, `target_at`, `actionable_from`, fixed event time, and scheduled work into one field.
2. Do not model travel as ordinary duplicate tasks.
3. Do not allow LLMs or connectors to write directly to canonical storage.
4. Preserve provenance, idempotency, reconciliation, and optimistic concurrency semantics.
5. Any automatic recommendation must remain explainable.
6. Add tests for every new time-dependent or reconciliation behavior.

## Pull requests

A PR should include:

- the problem being solved;
- affected invariants;
- schema/API changes;
- migration implications;
- focused tests;
- any user-visible semantic change.

Large architectural changes should update the normative specification first or in the same PR.
