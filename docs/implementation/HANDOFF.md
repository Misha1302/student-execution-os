# Student Execution OS — Implementation Handoff

> Checkpoint only. Re-read current repository, specification, PR/branch state, exact commit refs and CI before treating this file as current truth.

## Identity

- Repository: `Misha1302/student-execution-os`
- Pass 9 baseline: merged Pass 8 `f7d9f77d65e886326973eba01df1ca93e3ac734d`
- Baseline tree: `9cea664eafcaae17a1eb7b0fee86bfc7a55b3046`
- Normative specification: v2.1
- Pass 9 branch: `impl/pass-9-reliability-data-lifecycle`
- Schema: v7
- Package candidate: `0.6.0.dev1`
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
- [ ] Pass 9 — reliability / security / hardening; first data-lifecycle slice is the current candidate
- [ ] Pass 10 — conformance closure

## Current Pass 9 candidate

This slice establishes an executable data-recovery/export boundary before destructive account-lifecycle work.

### Backup / restore

- `SQLiteDataLifecycle.create_backup()` uses SQLite's online backup API for a consistent committed whole-database snapshot.
- Backup is written via a private temporary file then atomically renamed.
- Sidecar manifest format v1 records application version, UTC creation time, database filename, schema version, SHA-256, `integrity_check`, and account count.
- Restore fails closed on malformed/missing manifest, digest mismatch, integrity mismatch, account-count mismatch, or a schema newer than the running application.
- Restored data is copied into a private temporary database, migrated through the ordinary canonical migration owner, then checked with `PRAGMA integrity_check` and `PRAGMA foreign_key_check` before atomic replacement.
- Overwrite is never implicit; replacing an existing restore destination requires explicit authority.
- On POSIX, live SQLite files, backup files, manifests and exported JSON are forced to `0600`.

### Account export

- User export is not a database copy. `export_account(account_id)` is an explicit one-account JSON contract with a table/query allowlist.
- Child rows without their own `account_id` are reached only through an account-scoped parent.
- Export covers canonical state, evidence/provenance, reconciliation workflow, connector checkpoint/state, action/idempotency workflow, plan history, places/travel, recurrence and notification workflow.
- Exact saved private locations are intentionally included because this is the user's data-export artifact rather than the ordinary redacted Places API. UI explicitly marks the artifact as sensitive.
- Other accounts, schema migration metadata and connector/OAuth secrets are excluded.
- The export owner also compares the live schema to its classified-table set and fails closed when a future unclassified table appears. A future secret/data owner therefore cannot silently become exported or silently omitted without updating this contract.

### API / CLI / UI

- CLI commands: `backup`, `restore`, `account-export`, `reliability-smoke`.
- Server-bound `GET /api/v1/account/export` exports only the account already bound by the host; browser input cannot choose a different account.
- API responses are marked `Cache-Control: no-store`.
- Settings exposes the account-export action and warns that the downloaded artifact includes sensitive private account data, including exact saved locations.

## Acceptance coverage in this slice

- AT-61 — existing executable v6 → v7 migration fixture remains tracked as migration evidence.
- AT-62 — backup/restore recovers canonical state, provenance observations, connector checkpoint, notification workflow and action-idempotency replay, then successfully rebuilds planning input.
- AT-69 — one-account export includes the documented account state while excluding a second account and database-global migration metadata.
- Tampered backup bytes are rejected by SHA-256 before restore.
- Export fails closed if an unclassified future schema table exists.
- POSIX live database, backup and export artifacts are private-mode files.
- API export is server-account-scoped and no-store; browser Settings displays the sensitive-export trust contract.

## Verification checkpoint

Focused/current evidence before the final candidate-bound full run:

- Pass 9 reliability acceptance tests: **5/5 PASS**;
- web/API suite after this slice: **13/13 PASS**;
- Chromium UI suite after this slice: **3/3 PASS** in an isolated terminal run;
- an earlier full run reached **152/152 core PASS** and **13/13 API PASS** before an external tool timeout during Chromium; this is not treated as terminal full-suite evidence.

A final `make verify` on the completed candidate, including this handoff/documentation, is still required before commit/PR. Do not promote the earlier partial run into a green-candidate claim.

## Pass 9/10 gap ledger

See `docs/implementation/PASS9_GAP_LEDGER.md` for the fresh normative gap ledger. Major remaining work after this slice:

1. account deletion / retention / tombstone semantics (AT-63), reusing the data classification established by export;
2. durable notification delivery lease/outbox + crash/restart retry hardening;
3. cancel/reopen projection cleanup (AT-64);
4. hybrid occurrence representation (AT-65);
5. optional-event planning policy (AT-66);
6. observability / correlation and final install-restart-migration-export-deletion security closure in Pass 10.

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
