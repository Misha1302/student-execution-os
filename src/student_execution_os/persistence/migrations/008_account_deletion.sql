PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS account_deletion_tombstones (
    account_id TEXT PRIMARY KEY,
    deletion_id TEXT NOT NULL UNIQUE,
    deleted_at TEXT NOT NULL,
    purge_after TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    retained_reason TEXT NOT NULL,
    CHECK (length(trim(account_id)) > 0),
    CHECK (length(trim(deletion_id)) > 0),
    CHECK (length(trim(policy_version)) > 0),
    CHECK (length(trim(retained_reason)) > 0)
);

CREATE INDEX IF NOT EXISTS idx_account_deletion_tombstones_purge
ON account_deletion_tombstones(purge_after, account_id);
