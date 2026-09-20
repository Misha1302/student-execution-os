# ADR 0005 — Evidence, reconciliation, provenance, and cutoff ownership

- Status: Accepted for Pass 4
- Date: 2026-09-20

## Context

Pass 0–3 established canonical local state, sound feasibility, and deterministic planning, but external facts were not yet represented as immutable evidence. Pass 4 must add source provenance and reconciliation without creating a second writable truth owner or flattening uncertainty into one scalar deadline.

The critical ownership problem is `actual_cutoff`: the specification defines it as the resolved/local effective final hard cutoff, while unresolved external evidence may need a separate conservative `planning_cutoff`. Persisting an arbitrary chosen source value directly into the canonical Task would erase `CONFLICT` and could make risk incorrectly report `OVERDUE`.

## Decision

Pass 4 introduces a bounded reconciliation subsystem with these owners:

1. `SourceSystem`, `SourceRecord`, and `Observation` are immutable evidence/provenance.
2. `SourceBinding` is local reversible identity state; at most one binding is ACTIVE for one source-native entity.
3. `FieldPolicy` is immutable and versioned. Authority is evaluated per field and explicit source/source-context; unspecified authority fails closed.
4. `UserOverride` is explicit local interpretation state with ACTIVE/SUPERSEDED/REVOKED history.
5. `Conflict` is durable workflow state, not a competing fact.
6. `effective_fields` is materialized only by the reconciliation boundary. It retains effective state, policy version, evidence references, exact active `override_id` when overridden, conflict reference, and any separately labelled planning projection.

The first implemented reconciled critical field is `actual_cutoff`. Its effective state is one of `RESOLVED | OVERRIDDEN | ABSENT | CONFLICT | UNKNOWN`.

For cutoff conflicts, the field policy may project the earliest admissible exact cutoff for planning while the truth state remains `CONFLICT`. Risk reads both layers: a conflict that straddles `now` remains factual `UNKNOWN`; if every admissible exact cutoff has passed, `OVERDUE` may be emitted while conflict provenance remains visible.

## Single-owner transition

The persisted Task cutoff remains the backward-compatible/manual owner while no reconciliation effective field exists. Once reconciliation materializes `actual_cutoff` for a Task, direct canonical cutoff mutation is rejected. Subsequent user changes use an explicit reconciliation override. `target_at` remains a separate canonical user-owned planning goal and stays independently writable.

Planning reads a derived Task copy containing the effective/resolved or conservative cutoff projection and also carries a `CutoffReconciliationContext` with unresolved truth/provenance. The canonical Task row is not rewritten from source evidence.

## Revision and invalidation policy

Evidence ingestion, source availability history, and provenance-only changes do not automatically advance the canonical planning revision. A reconciliation change advances `server_revision` only when material effective/planning semantics or the governing policy version changes.

`PlanningSnapshot.input_hash` includes material reconciliation inputs and policy version, but not opaque evidence/conflict identifiers. Provenance remains inspectable in the snapshot context without causing plan churn when semantically equivalent evidence revisions arrive.

## Source freshness and deletion

For a source-native entity/field, source-current evidence is chosen by declared revision order when available, then observation freshness. A late older revision cannot regress current state.

STALE/UNAVAILABLE source status is recorded separately and never implies deletion. Explicit provider deletion creates source-removal evidence, transitions the binding to `SOURCE_REMOVED`, and reruns reconciliation; it never hard-deletes the local Task/Event.

## Security boundary

Source, Extractor, and Actor are distinct identities. Imported/retrieved content is data only. It cannot become an actor or authorize a mutation. User overrides require an authenticated user-origin actor. Raw payload retention remains optional and policy-controlled.

## Consequences

- Pass 5 can implement one real connector against stable evidence/reconciliation contracts.
- External cutoff conflicts remain visible while planning can proceed conservatively where policy permits.
- Existing manual-only Tasks remain compatible until reconciliation takes ownership of the cutoff field.
- A general event-sourcing framework, plugin framework, provider connector, queue, or vector store is intentionally not introduced in Pass 4.
