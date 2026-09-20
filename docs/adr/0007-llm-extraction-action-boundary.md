# ADR 0007 — LLM extraction and authenticated action boundary

- Status: Accepted for Pass 6
- Date: 2026-09-21

## Context

The specification separates LLM extraction from mutation authority. Imported/source content is untrusted evidence; an LLM may parse it, but the content itself cannot become an actor, grant permissions, or substitute for authenticated user intent.

Pass 6 therefore proves the boundary before adding a live LLM provider or broad tool surface.

## Decision

### 1. Extraction is tool-less

`ToollessExtractionContext` accepts source-bound content plus an extractor implementation and returns typed `ExtractionObservationCandidate` values only.

It owns no canonical repository, action gateway, authenticated principal, or mutation tool.

A trusted server-side `ExtractionIngestor` may persist the resulting typed proposal as immutable observations. The persisted actor is server/system-owned; the imported text and model are not actors.

Deterministic observation identities make repeated ingestion replay-safe.

### 2. Action requests carry no self-asserted actor or target authority

The LLM-facing action request contains only server-bound references equivalent to:

- `intent_id`
- `idempotency_key`
- `expected_version`
- optional `dry_run`

Account, principal, client scope, command, target entity, intent strength, and authorization status come from durable server state.

`AuthenticatedPrincipal` is an application/server input to the gateway. It is not part of the model request schema.

### 3. Destructive action authorization is server-minted and scoped

Pass 6 intentionally implements one destructive command deeply: `CANCEL_OBLIGATION`.

A durable `ActionIntent` binds:

- account;
- authenticated principal;
- client;
- one command;
- one target entity;
- one expected entity version;
- intent strength;
- confirmation state.

`EXPLICIT_SCOPED` intent may execute directly. `AMBIGUOUS` or `INFERRED` intent cannot execute until a separate authenticated confirmation is recorded.

Imported/retrieved content never creates or confirms an intent.

### 4. Mutation correctness remains canonical-repository owned

The action gateway does not implement a second lifecycle transition owner.

`SQLiteCanonicalRepository._transition_obligation_in_tx` is the mutation owner used both by ordinary repository methods and the action gateway. It preserves canonical optimistic concurrency and canonical audit/change behavior.

LLM-originated committed mutation is recorded with `ActorCategory.USER_VIA_LLM` and the server intent id in the canonical audit payload.

### 5. Idempotency and mutation are one transaction

Action idempotency scope is:

`account + principal + client + command family + idempotency key`.

Within one SQLite transaction the gateway:

1. checks semantic request fingerprint;
2. validates current server intent;
3. performs the canonical mutation with expected version;
4. consumes the intent;
5. stores the replay result.

The same key + same fingerprint replays the original logical result without re-running the mutation, even if the entity later changes.

The same key + different semantic request fails with `IdempotencyConflict`.

### 6. Authorization lifecycle is separately auditable

`action_intent_history` records MINTED, CONFIRMED, and CONSUMED transitions without advancing planning revision merely because authorization workflow state changed.

The actual canonical mutation remains in the canonical audit log.

### 7. Private place data is withheld by default

`PrivatePlace.llm_context()` exposes the opaque alias only.

Exact address/coordinates are returned only when the server provides an `ExactLocationGrant` bound to a concrete operation. The model-facing default cannot reveal the secret fields by requesting a boolean flag.

### 8. Cross-account isolation is inherited and rechecked at the gateway

Intent lookup, target lookup, read access, idempotency scope, and mutation all use the authenticated principal's account scope.

A guessed entity or intent id from another account is indistinguishable from not found.

## Schema

SQLite schema v5 adds:

- `action_intents`;
- `action_idempotency_records`;
- `action_intent_history`.

Migration coverage verifies v4 → v5 preservation of existing canonical state.

## Verification contract

Pass 6 executable coverage includes:

- AT-41 stale expected version cannot overwrite newer state;
- AT-42 same idempotency request replays the original result;
- AT-43 idempotency-key semantic reuse conflicts;
- AT-44 restart preserves durable action replay state;
- AT-45 prompt-injection-like imported text can become evidence but not mutation authority;
- AT-46 ambiguous destructive intent requires new authenticated confirmation;
- AT-47 one explicit scoped cancellation executes with normal version/idempotency checks;
- AT-48 HOME-style alias hides exact address/coordinates by default;
- AT-49 account A cannot read or mutate account B through the gateway.

`agent-smoke` is part of `make verify` and CI.

## Strongest alternative considered

A generic LLM tool/plugin framework with arbitrary command schemas would make the boundary broader before a second real action family proves the abstraction.

That alternative is rejected for Pass 6. One command plus typed extraction is enough to prove authorization, idempotency, concurrency, audit, privacy, and prompt-injection boundaries without creating a premature public plugin ABI.

## Consequences

- A live LLM provider can be added later without changing who owns authorization.
- Imported source content can be processed by the same underlying model provider in a separate call, but not with the action capability/context.
- Pass 7 can introduce Places/travel while preserving exact-location disclosure behind the same server privacy boundary.
- Pass 6 does not claim that Python types are an authentication mechanism; production transport authentication remains a server/API responsibility.
