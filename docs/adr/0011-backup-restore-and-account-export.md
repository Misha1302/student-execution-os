# ADR 0011 — SQLite backup/restore and account export boundary

- Status: Accepted for Pass 9 reliability/data-lifecycle slice
- Date: 2026-09-21
- Specification: v2.1 §§24.2, 25.1–25.3; AT-61, AT-62, AT-69

## Context

Passes 0–8 persist canonical state, immutable evidence/provenance, connector checkpoints, action idempotency, plans, travel, recurrence and notification workflow in one account-scoped SQLite database. A green application test suite is not a recovery strategy: a release needs an executable way to take a consistent snapshot and prove that the restored state can resume planning/reconciliation without silent duplication or lost workflow state.

Data export is a different ownership problem. Backup is an operator recovery artifact for the whole database. User export is an explicit, one-account data contract and must never become “copy the database and hope the caller filters it”.

## Decision

### Full-database backup is the recovery unit

`SQLiteDataLifecycle.create_backup()` uses SQLite’s online backup API to copy one consistent committed database snapshot. The backup includes all persisted owners together: canonical local state, evidence/provenance, connector state/checkpoints, idempotency records, plans, places/travel, recurrence and notification workflow.

The backup is written through a private temporary file and atomically renamed. On POSIX the database, backup, manifest and account-export files are forced to mode `0600`.

Each backup has a sidecar manifest, format version 1, containing:

- application version;
- UTC creation time;
- backup filename;
- schema version;
- SHA-256 of the backup bytes;
- SQLite `integrity_check` result;
- account count.

A backup file without a valid manifest is not accepted by the restore command.

### Restore validates before replacement

`restore_backup()` verifies the manifest shape/version, SHA-256, SQLite integrity and declared schema version before copying. A backup newer than the current application schema fails closed. The restored temporary database is then migrated forward through the normal canonical migration owner, checked with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`, and only then atomically renamed to the requested destination.

Overwrite is never implicit. Replacing an existing destination requires the explicit `--overwrite` CLI flag.

The restore test must demonstrate more than row survival: restored provenance, connector checkpoint, action-idempotency replay and notification workflow must remain usable, and planning must be able to construct a new snapshot from restored state.

### Account export is an explicit allowlist

`SQLiteDataLifecycle.export_account()` emits JSON export format version 1. It starts from the server-bound `account_id` and uses an explicit table/query allowlist. Child tables without their own `account_id` (`tasks`, `events`, histories, `plan_blocks`) are filtered through their account-scoped parent.

The export includes:

- account-scoped canonical local state;
- evidence/provenance and reconciliation workflow;
- connector state/checkpoints;
- action/idempotency workflow;
- plan history;
- places/travel, including exact saved location data;
- recurrence and notification workflow.

It excludes:

- rows belonging to any other account;
- database-global migration metadata;
- connector/OAuth secrets and AI provider keys. Encrypted per-account AI keys (`llm_credentials`, ADR 0017) are classified as credentials and never exported, not even as ciphertext; any future secret table is *not* automatically exported because the allowlist must be revised explicitly.

Because the user-data export intentionally includes private account data, the normal Places API redaction rule does not apply to the export artifact. The Settings UI states this difference before offering the export link.

### Production boundary

This ADR proves a local recovery/export contract; it does not claim production deployment readiness. A production backup store must provide access control/encryption appropriate to the primary data. Future external secret storage must define its own revocation and recovery procedure and may not be inferred from the SQLite backup.

## Rejected alternatives

### Copy the SQLite file directly

Rejected. Plain file copy can miss a transaction/WAL consistency boundary and provides no manifest/hash/restore proof.

### Per-table backup

Rejected. It creates ordering/foreign-key/revision hazards and can split connector/idempotency/workflow state from the canonical snapshot that justifies it.

### Use the full database as user export

Rejected. It would leak other accounts and database-global/internal state, and future secret tables could silently become exportable.

## Verification

- AT-61 remains covered by the executable v6 → v7 migration fixture.
- AT-62 restores canonical state plus provenance, checkpoint, idempotency and notification workflow and successfully rebuilds planning input.
- AT-69 proves a one-account export contains the documented account data and not the second account.
- Tampered backup bytes fail SHA-256 validation before restore.
- POSIX persistence/backup/export artifacts are private-mode files.
