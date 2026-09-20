# ADR 0001 — Initial implementation stack

- Status: Accepted for Pass 0
- Date: 2026-09-20

## Context

The repository was specification-only at Pass 0. The implementation needs a runnable skeleton now and must leave room for framework-independent domain invariants, SQLite-backed persistence, exact/sound feasibility, and deterministic planning in later passes.

## Decision

Use Python 3.12+ with a `src/` package layout. Keep the runtime dependency set empty in Pass 0 and use the standard library for the executable CLI and tests. Use `unittest` for the initial harness and GitHub Actions on Python 3.13 for CI. Pass 0 runs from source via `PYTHONPATH=src`, avoiding a packaging backend dependency before packaging is a product need.

The package boundaries are initially `application`, `domain`, `planning`, and `persistence`. They establish ownership only; Pass 0 does not add fake domain/planning implementations.

## Why

1. The current execution environment has Python 3.13 available and no .NET SDK, so this stack can be built and verified from the exact implementation session.
2. Standard-library-only runtime keeps the first pass reversible and removes framework/solver choices that the specification intentionally leaves open.
3. Python's `sqlite3`, timezone support, dataclasses/types, and test ecosystem are sufficient for the next domain/persistence and planning passes without requiring a structural rewrite.

## Strongest alternative considered

C#/.NET would provide stronger compile-time modeling and is a credible long-term alternative, especially for a domain-heavy system. It was not selected for Pass 0 because the current verification environment lacks a .NET SDK and adopting it would make this pass depend on unverified tooling rather than product semantics. This ADR does not freeze a public language/API contract; changing stack later requires an explicit migration ADR and preservation of normative behavior.

## Consequences

- Production domain semantics must remain explicit rather than relying on dynamic typing accidents.
- CI is the canonical clean Linux verification environment.
- New third-party dependencies require a concrete requirement and review; they are not added speculatively.
