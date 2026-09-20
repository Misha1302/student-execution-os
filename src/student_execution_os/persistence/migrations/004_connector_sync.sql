CREATE TABLE IF NOT EXISTS connector_states (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_system_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    scope TEXT NOT NULL,
    checkpoint TEXT,
    health_status TEXT NOT NULL CHECK (health_status IN ('CURRENT','STALE','UNAVAILABLE')),
    last_successful_complete_sync_at TEXT,
    latest_failure_reason TEXT,
    connector_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(account_id, id),
    UNIQUE(account_id, source_system_id, provider, scope),
    FOREIGN KEY(account_id, source_system_id) REFERENCES source_systems(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS connector_sync_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    cursor_before TEXT,
    state_version_before INTEGER NOT NULL CHECK (state_version_before >= 1),
    cursor_after TEXT,
    is_full_sync INTEGER NOT NULL CHECK (is_full_sync IN (0,1)),
    status TEXT NOT NULL CHECK (status IN ('COMPLETE','PARTIAL','FAILED')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    record_count INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    deletion_count INTEGER NOT NULL DEFAULT 0 CHECK (deletion_count >= 0),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, connector_id) REFERENCES connector_states(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_connector_sessions
ON connector_sync_sessions(account_id, connector_id, started_at, id);

CREATE TABLE IF NOT EXISTS connector_entities (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    external_entity_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','REMOVED')),
    last_provider_revision TEXT NOT NULL,
    last_source_record_id TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id, connector_id, external_entity_id),
    FOREIGN KEY(account_id, connector_id) REFERENCES connector_states(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, last_source_record_id) REFERENCES source_records(account_id, id)
);

CREATE TABLE IF NOT EXISTS connector_ingestion_receipts (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    external_entity_id TEXT NOT NULL,
    provider_revision TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY(account_id, connector_id, external_entity_id, provider_revision),
    FOREIGN KEY(account_id, connector_id) REFERENCES connector_states(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, source_record_id) REFERENCES source_records(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_connector_receipts_record
ON connector_ingestion_receipts(account_id, source_record_id);
