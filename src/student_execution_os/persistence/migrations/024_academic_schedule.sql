-- Schema v24: user-connected academic schedule providers.
--
-- The feed address is credential material (it commonly contains a bearer token). It is
-- encrypted with a dedicated key outside the database and is never returned by the API or
-- account export. Canonical classes remain owned by SourceApplier (schema v23).
CREATE TABLE academic_schedule_connections (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    source_system_id TEXT NOT NULL,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) BETWEEN 1 AND 120),
    mode TEXT NOT NULL CHECK (mode IN ('URL','UPLOAD','DISCONNECTED')),
    default_timezone TEXT NOT NULL,
    feed_ciphertext BLOB,
    feed_nonce BLOB,
    feed_key_id TEXT,
    feed_host TEXT,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 60
        CHECK (sync_interval_minutes BETWEEN 15 AND 1440),
    next_sync_at TEXT,
    last_content_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(account_id, connector_id),
    UNIQUE(account_id, source_system_id),
    FOREIGN KEY(account_id, connector_id)
        REFERENCES connector_states(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, source_system_id)
        REFERENCES source_systems(account_id, id) ON DELETE CASCADE,
    CHECK (
      (mode='URL' AND feed_ciphertext IS NOT NULL AND feed_nonce IS NOT NULL
                  AND feed_key_id IS NOT NULL AND feed_host IS NOT NULL
                  AND next_sync_at IS NOT NULL)
      OR (mode IN ('UPLOAD','DISCONNECTED') AND feed_ciphertext IS NULL
                  AND feed_nonce IS NULL AND feed_key_id IS NULL AND feed_host IS NULL
                  AND next_sync_at IS NULL)
    )
);

CREATE INDEX idx_academic_schedule_due
ON academic_schedule_connections(mode, next_sync_at);
