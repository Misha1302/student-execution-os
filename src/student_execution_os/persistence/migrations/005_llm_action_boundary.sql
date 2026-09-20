CREATE TABLE IF NOT EXISTS action_intents (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('CANCEL_OBLIGATION')),
    target_entity_id TEXT NOT NULL,
    intent_strength TEXT NOT NULL CHECK (intent_strength IN ('EXPLICIT_SCOPED','AMBIGUOUS','INFERRED')),
    expected_version INTEGER NOT NULL CHECK (expected_version >= 1),
    requires_confirmation INTEGER NOT NULL CHECK (requires_confirmation IN (0,1)),
    confirmed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING','CONSUMED','REJECTED')),
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, target_entity_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_action_intents_principal
ON action_intents(account_id, principal_id, client_id, status, created_at);

CREATE TABLE IF NOT EXISTS action_idempotency_records (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    command_family TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    result_entity_id TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version >= 1),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, principal_id, client_id, command_family, idempotency_key),
    FOREIGN KEY(account_id, intent_id) REFERENCES action_intents(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, result_entity_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_action_idempotency_result
ON action_idempotency_records(account_id, result_entity_id, created_at);
CREATE TABLE IF NOT EXISTS action_intent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    intent_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('MINTED','CONFIRMED','CONSUMED','REJECTED')),
    recorded_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY(account_id, intent_id) REFERENCES action_intents(account_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_action_intent_history
ON action_intent_history(account_id, intent_id, id);
