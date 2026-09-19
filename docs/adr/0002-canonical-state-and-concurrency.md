# ADR 0002 — Canonical local state, SQLite persistence, and concurrency ownership

- Status: Accepted for Pass 1
- Date: 2026-09-20

## Context

Pass 1 must establish canonical local Task/Event/Project state before PlanningSnapshot and feasibility exist. The specification requires account scoping, explicit aggregate versions, a monotonic committed `server_revision`, append-only change/audit evidence, and a single concurrency owner for every mutation.

The most dangerous ambiguity is subtype ownership: `Task` and `Event` carry fields that change, but they are not independent aggregate roots from `Obligation`. Giving subtype tables their own writable version would make one logical obligation have two concurrency owners and would violate AT-80.

## Decision

Use the Python standard library `sqlite3` adapter behind the framework-independent `CanonicalRepository` port.

Persistence ownership is:

- `Obligation` owns the version for its `Task` or `Event` subtype row;
- `Project`, `Milestone`, and `UserTimeConstraint` are independently versioned roots in the current scope;
- `Dependency` and project membership are mutated only through repository commands and do not expose an independent update API yet;
- every planning-relevant committed mutation increments the account's monotonic `server_revision` and appends one `audit_changes` row in the same transaction;
- all public repository reads/writes are account-scoped and return `EntityNotFound` rather than revealing an entity from another account;
- datetimes must be offset-aware and are normalized to UTC for storage;
- Event/task occupancy intervals use `[start,end)` semantics;
- current release supports only `FIXED_INTERVAL` Event semantics; `FLEXIBLE_WINDOW` is rejected as unsupported rather than coerced;
- the Obligation `actual_cutoff` is the canonical final-cutoff owner for an Obligation, so an Obligation-owned `FINAL_CUTOFF` Milestone is rejected in both domain logic and schema constraints. Project final gates remain allowed when they are genuinely project-owned.

Schema changes are versioned through `schema_migrations`; Pass 1 introduces schema version 1 in `src/student_execution_os/persistence/migrations/001_initial.sql`.

## Strongest alternative considered

A second version column on `tasks`/`events` would make local updates easier to express but creates dual ownership and permits stale parent state to coexist with a newer subtype. That alternative is rejected because the specification explicitly requires one concurrency owner and AT-80 requires stale parent preconditions to fail without overwrite.

An ORM/framework was also unnecessary for this pass: it would add migration/runtime dependencies without improving the ownership proof. The repository port keeps replacement possible later.

## Consequences

- Pass 2 can build PlanningSnapshot from one revisioned canonical state model without first repairing subtype concurrency.
- Direct SQL is an adapter implementation detail, not a public mutation surface.
- Authentication/authorization is not claimed here; account scoping is enforced at the repository boundary, while principal derivation remains a later application/security responsibility.
- Idempotency keys are intentionally deferred to the dedicated sync/reliability scope; optimistic concurrency is implemented now.
