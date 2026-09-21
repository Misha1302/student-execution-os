# Student Execution OS — Implementation Handoff

> Checkpoint only. Re-read current repository, specification, PR/branch state, exact commit refs and CI before treating this file as current truth.

## Identity

- Repository: `Misha1302/student-execution-os`
- Pass 9 deletion baseline: merged recovery/export slice `8498d67d36bf8cbec1fc03ce84324bc66b87423c`
- Baseline tree: `0a5b3c4e7cf9f9358cfd279c38a298fedc0b465b`
- Normative specification: v2.1
- Pass 9 branch: `impl/pass-9-account-deletion`
- Schema: v8
- Package candidate: `0.6.0.dev2`
- Date: 2026-09-21

## Completed passes

- [x] Pass 0 — baseline / stack / skeleton / CI
- [x] Pass 1 — canonical local domain / persistence / concurrency
- [x] Pass 2 — immutable PlanningSnapshot / sound tri-state feasibility
- [x] Pass 3 — planner / risk / PlanSnapshot / next actions
- [x] Pass 4 — evidence / reconciliation / provenance
- [x] Pass 5 — Google Calendar evidence connector
- [x] Pass 6 — LLM extraction / authenticated action boundary
- [x] Pass 7 — travel-aware planning
- [x] Product UI stage — PR #10 merged; post-merge CI green
- [x] Pass 8 — recurrence / notification workflow; PR #11 merged at `f7d9f77d65e886326973eba01df1ca93e3ac734d`; post-merge run #96 green
- [ ] Pass 9 — reliability / security / hardening; recovery/export merged, account-deletion slice is the current candidate
- [ ] Pass 10 — conformance closure

## Current Pass 9 candidate

This slice implements the normative AT-63 account-deletion/retention contract on top of the already merged backup/restore + export owner.

### Account deletion policy v1

- `SQLiteDataLifecycle.delete_account()` is a destructive data-lifecycle command, not a Task/Event lifecycle mutation.
- The command is bound to one account, requires that account's current `server_revision`, and requires the exact account id as a second typed confirmation.
- All account-scoped SQLite operational/private state is purged immediately through the canonical FK graph. The owner verifies all directly account-scoped tables are empty and `PRAGMA foreign_key_check` is clean before commit.
- The only retained local state is a 30-day `account_deletion_tombstones` row containing account id, deletion receipt id/timestamps, policy version and retention reason. It exists only to reject stale connector/client replay and account-id reuse while deletion propagates.
- Audit/provenance rows are not retained by this local policy. The current release has no raw payload store and no OAuth/token secret store, so secret revocation is explicitly `NOT_APPLICABLE_NO_SECRET_STORE`.
- Any future unclassified database table makes deletion fail closed until the data-lifecycle owner is deliberately extended.
- `create_account()` rejects reuse while the tombstone is live; expired tombstones may be purged explicitly before reprovisioning.

### API / CLI / UI

- CLI: `account-delete --expected-revision ... --confirm-account ...` and `deletion-tombstones-purge`.
- `GET /api/v1/account/deletion-policy` exposes the current policy and server revision for the server-bound account.
- `POST /api/v1/account/delete` accepts only expected revision + typed confirmation; a browser cannot select another account.
- Settings explains immediate purge versus the minimal 30-day tombstone before exposing the destructive confirmation. Browser QA only previews/cancels the action.
- `reliability-smoke` now also performs a separate account deletion and reports the policy/tombstone expiry.

### Acceptance coverage

- AT-63 — account deletion removes account-owned operational/private data, preserves another account, leaves no FK orphan, retains only the documented tombstone, blocks account-id reuse during retention and permits explicit reuse after purge.
- Wrong typed confirmation, stale expected revision and future unclassified tables all fail closed.
- API proof verifies that a client-supplied alternate `account_id` cannot redirect deletion away from the server-bound account.
- Migration v7 → v8 preserves existing notification/account state while adding the tombstone table.

### Verification checkpoint

Final candidate-bound verification evidence:

- core unit/integration/acceptance discovery: **156/156 PASS**;
- web/API suite: **14/14 PASS**;
- Chromium desktop/mobile UI suite: **3/3 PASS**;
- all CLI smoke surfaces PASS, including schema-v8 `reliability-smoke` with deletion policy v1;
- web-host smoke PASS;
- `git diff --check` PASS;
- added-lines secret-like scan: no findings.

The full candidate-bound `make verify` terminated with exit code **0**. Before remote mutation, still re-read live `main`, build an exact GitHub tree from these candidate bytes, and require hosted exact-head CI before merge.

## Pass 9/10 gap ledger

See `docs/implementation/PASS9_GAP_LEDGER.md` for the fresh normative gap ledger. Major remaining work after this slice:

1. durable notification delivery lease/outbox + crash/restart retry hardening;
2. cancel/reopen projection cleanup (AT-64);
3. hybrid occurrence representation (AT-65);
4. optional-event planning policy (AT-66);
5. observability / correlation and final install-restart-migration-export-deletion security closure in Pass 10.

## Production boundary / known external limits

This candidate proves local SQLite recovery/export semantics; it does **not** claim production deployment readiness.

Still intentionally outside the present implementation:

- production authentication and TLS termination;
- encrypted/remote backup storage and operator key management;
- production OAuth consent, token encryption, revocation/rotation and secret-store integration;
- a live external notification channel;
- live routing/maps provider;
- live LLM provider.

A production backup store must provide access control and encryption appropriate to the primary data. Future externally stored secrets need their own deletion/revocation/recovery contract and must not be inferred from this SQLite backup/export boundary.

## Next action

Run terminal candidate-bound `make verify`, `git diff --check`, repository hygiene/secret scan and adversarial ownership review. Only after those are green should this slice be committed, reproduced as an exact GitHub tree over the current live `main`, opened as a PR, exact-head CI checked, and merged with a fresh base/head/mergeability check followed by post-merge CI.
