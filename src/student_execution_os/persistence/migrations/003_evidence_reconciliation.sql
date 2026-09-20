CREATE TABLE IF NOT EXISTS source_systems (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    policy_context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, id)
);

CREATE TABLE IF NOT EXISTS source_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_system_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','STALE','UNAVAILABLE')),
    recorded_at TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    FOREIGN KEY(account_id, source_system_id) REFERENCES source_systems(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_status_history ON source_status_history(account_id, source_system_id, id);

CREATE TABLE IF NOT EXISTS source_records (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_system_id TEXT NOT NULL,
    external_entity_id TEXT,
    source_revision TEXT,
    revision_order INTEGER,
    observed_at TEXT NOT NULL,
    content_hash TEXT,
    source_uri TEXT,
    raw_payload_ref TEXT,
    metadata_json TEXT NOT NULL,
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, source_system_id) REFERENCES source_systems(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_records_native ON source_records(account_id, source_system_id, external_entity_id, revision_order, observed_at);

CREATE TABLE IF NOT EXISTS source_bindings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_system_id TEXT NOT NULL,
    external_entity_id TEXT NOT NULL,
    local_entity_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','DETACHED','SOURCE_REMOVED')),
    match_decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, source_system_id) REFERENCES source_systems(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, local_entity_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_binding_active_owner
ON source_bindings(account_id, source_system_id, external_entity_id) WHERE state='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_source_bindings_local ON source_bindings(account_id, local_entity_id, state);

CREATE TABLE IF NOT EXISTS binding_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id TEXT NOT NULL REFERENCES source_bindings(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','DETACHED','SOURCE_REMOVED')),
    recorded_at TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    reason TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_record_id TEXT NOT NULL,
    binding_id TEXT,
    field_path TEXT NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN ('HARD_CUTOFF','BOOLEAN','STRING','ABSENT','SOURCE_REMOVED')),
    value_json TEXT NOT NULL,
    extraction_certainty TEXT NOT NULL CHECK (extraction_certainty IN ('EXACT','HIGH','MEDIUM','LOW','UNKNOWN')),
    observed_at TEXT NOT NULL,
    extractor_id TEXT NOT NULL,
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, source_record_id) REFERENCES source_records(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, binding_id) REFERENCES source_bindings(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_observations_field ON observations(account_id, field_path, source_record_id);

CREATE TABLE IF NOT EXISTS reconciliation_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    field_path TEXT NOT NULL,
    version TEXT NOT NULL,
    min_certainty TEXT NOT NULL CHECK (min_certainty IN ('EXACT','HIGH','MEDIUM','LOW','UNKNOWN')),
    source_authority_json TEXT NOT NULL,
    freshness_rule TEXT NOT NULL CHECK (freshness_rule='REVISION_THEN_OBSERVED_AT'),
    allow_user_override INTEGER NOT NULL CHECK (allow_user_override IN (0,1)),
    override_is_hard_planning_input INTEGER NOT NULL CHECK (override_is_hard_planning_input IN (0,1)),
    conflict_projection TEXT NOT NULL CHECK (conflict_projection IN ('NONE','EARLIEST_HARD_CUTOFF')),
    created_at TEXT NOT NULL,
    UNIQUE(account_id, field_path, version)
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_policy_current ON reconciliation_policies(account_id, field_path, id);

CREATE TABLE IF NOT EXISTS user_overrides (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entity_ref TEXT NOT NULL,
    field_path TEXT NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN ('HARD_CUTOFF','ABSENT','BOOLEAN','STRING')),
    value_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED')),
    reason TEXT,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, entity_ref) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_override
ON user_overrides(account_id, entity_ref, field_path) WHERE status='ACTIVE';

CREATE TABLE IF NOT EXISTS override_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    override_id TEXT NOT NULL REFERENCES user_overrides(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED')),
    recorded_at TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    reason TEXT
);

CREATE TABLE IF NOT EXISTS reconciliation_conflicts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entity_ref TEXT NOT NULL,
    field_path TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED','DISMISSED','SUPERSEDED')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_ref TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, entity_ref) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_reconciliation_conflict
ON reconciliation_conflicts(account_id, entity_ref, field_path) WHERE status='OPEN';

CREATE TABLE IF NOT EXISTS conflict_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_id TEXT NOT NULL REFERENCES reconciliation_conflicts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('OPEN','RESOLVED','DISMISSED','SUPERSEDED')),
    recorded_at TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    resolution_ref TEXT
);

CREATE TABLE IF NOT EXISTS effective_fields (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entity_ref TEXT NOT NULL,
    field_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESOLVED','OVERRIDDEN','ABSENT','CONFLICT','UNKNOWN')),
    selected_value_type TEXT,
    selected_value_json TEXT,
    evidence_ids_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    planning_projection_json TEXT,
    admissible_cutoffs_json TEXT NOT NULL,
    reason TEXT,
    override_id TEXT REFERENCES user_overrides(id) ON DELETE SET NULL,
    conflict_id TEXT REFERENCES reconciliation_conflicts(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, entity_ref, field_path),
    FOREIGN KEY(account_id, entity_ref) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS effective_field_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entity_ref TEXT NOT NULL,
    field_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('RESOLVED','OVERRIDDEN','ABSENT','CONFLICT','UNKNOWN')),
    selected_value_type TEXT,
    selected_value_json TEXT,
    evidence_ids_json TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    planning_projection_json TEXT,
    admissible_cutoffs_json TEXT NOT NULL,
    reason TEXT,
    override_id TEXT REFERENCES user_overrides(id) ON DELETE SET NULL,
    conflict_id TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effective_history_entity ON effective_field_history(account_id, entity_ref, field_path, id);

CREATE TABLE IF NOT EXISTS idempotency_records (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_entity_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, operation, idempotency_key),
    FOREIGN KEY(account_id, result_entity_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reconciliation_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    entity_ref TEXT,
    committed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_audit_account ON reconciliation_audit(account_id, id);

CREATE TRIGGER IF NOT EXISTS source_systems_no_update
BEFORE UPDATE ON source_systems BEGIN
    SELECT RAISE(ABORT, 'source_systems are immutable; record status/history separately');
END;

CREATE TRIGGER IF NOT EXISTS source_records_no_update
BEFORE UPDATE ON source_records BEGIN
    SELECT RAISE(ABORT, 'source_records are immutable evidence');
END;

CREATE TRIGGER IF NOT EXISTS observations_no_update
BEFORE UPDATE ON observations BEGIN
    SELECT RAISE(ABORT, 'observations are immutable evidence');
END;

CREATE TRIGGER IF NOT EXISTS reconciliation_policies_no_update
BEFORE UPDATE ON reconciliation_policies BEGIN
    SELECT RAISE(ABORT, 'reconciliation policies are immutable/versioned');
END;
