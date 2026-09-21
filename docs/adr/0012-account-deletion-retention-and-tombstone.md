# ADR 0012 — Account deletion, retention, and replay tombstone

- Status: Accepted for Pass 9 local data-lifecycle slice
- Date: 2026-09-21
- Specification: v2.1 §24.2 / AT-63

## Context

Account deletion is a destructive data-lifecycle operation, not an ordinary Task/Event lifecycle command. A blind `DELETE FROM accounts` is insufficient because the product needs an explicit answer for retained evidence/audit state, connector/client replay, account-id reuse, future secret stores, cross-account isolation, and optimistic concurrency.

The current implementation stores all operational product state in one local SQLite database. It does **not** yet have a production OAuth/token secret store, raw payload archive, remote backup service, or legal/compliance retention owner. The local deletion policy therefore must not invent such infrastructure or imply a production retention guarantee.

## Decision

### Policy v1

`account-deletion-v1` performs an immediate hard purge of all account-scoped operational/private SQLite state after all of the following hold:

1. the command is bound to the server-selected account;
2. the caller supplies that account's current `server_revision`;
3. the caller types the exact account id as a separate confirmation value;
4. every live database table is classified by the data-lifecycle owner.

The account row is then deleted under the expected revision. Existing foreign keys cascade through account-owned state. The owner verifies that every directly account-scoped table is empty for that account and that `PRAGMA foreign_key_check` is clean before commit.

### Minimal deletion tombstone

For 30 days by default, the only retained local state is one row in `account_deletion_tombstones` containing:

- `account_id`;
- `deletion_id`;
- `deleted_at`;
- `purge_after`;
- `policy_version`;
- `retained_reason`.

The tombstone exists only to reject stale connector/client replay and immediate account-id reuse while deletion propagates. It is not a recoverable account copy, is not returned in user account export, and contains no Task/Event content, evidence values, private place coordinates/address, plan data, notification content, or connector payload.

Before `purge_after`, `create_account()` rejects reuse of the same account id. After expiry, the tombstone may be purged and the id may be explicitly provisioned again.

### Evidence, audit, and secrets

This local policy retains **no** account audit/provenance rows after deletion. The current release has no raw-source payload store and no OAuth/token secret store, so secret revocation is reported as `NOT_APPLICABLE_NO_SECRET_STORE` rather than fabricated.

Future raw-payload, secret, legal-retention, or remote-replica tables are fail-closed: account deletion refuses to run until the data-lifecycle table classification and this policy are deliberately extended. Production OAuth/token revocation and external replica deletion remain separate deployment/integration obligations.

### Surfaces

- CLI: `account-delete` requires database, account id, expected revision and exact account confirmation; retention duration can be configured explicitly. `deletion-tombstones-purge` removes expired tombstones.
- HTTP: `GET /api/v1/account/deletion-policy` exposes the current server-bound policy/revision. `POST /api/v1/account/delete` accepts only expected revision + typed confirmation; the client cannot choose another account.
- UI: Settings shows the immediate-purge/30-day-tombstone semantics before confirmation. Execution requires typing the exact account id. Browser QA previews/cancels this operation rather than deleting the fixture account.

## Alternatives considered

### Cascade-only account delete

Rejected. It leaves retention/replay/account-id reuse and future unclassified stores undefined and can silently become incomplete as schema owners evolve.

### Retain all audit/provenance indefinitely

Rejected for the current local product. It would contradict the requested hard-account-deletion semantics and create a privacy retention policy without a legal/product owner. A future regulated deployment may adopt a different explicit policy with separately justified retained fields and access controls.

### Keep a full deleted-account backup/tombstone

Rejected. A tombstone intended only for replay suppression must not become a shadow copy of private account data.

## Verification contract

AT-63 acceptance must prove that:

- all account-owned operational/private rows disappear;
- another account remains intact;
- no orphan foreign-key rows remain;
- only the documented tombstone fields remain;
- stale revision and wrong typed confirmation fail closed;
- an unclassified future table blocks deletion;
- account-id reuse is blocked during retention and allowed only after tombstone purge;
- server API ignores any client-supplied alternate `account_id` and acts only on the host-bound account.

## Consequences

Account deletion is now an explicit, versioned, auditable product contract for the local SQLite release. It is still **not** a claim that external OAuth providers, remote backups, external notification providers, or offline replicas have been deleted; those systems need their own authenticated deletion/revocation acknowledgements before production closure.
