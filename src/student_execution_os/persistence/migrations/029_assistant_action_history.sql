CREATE TABLE assistant_action_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    apply_idempotency_key TEXT NOT NULL,
    action_id TEXT NOT NULL,
    sequence_index INTEGER NOT NULL CHECK (sequence_index >= 0),
    command TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    committed_version INTEGER NOT NULL CHECK (committed_version >= 1),
    inverse_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    undone_at TEXT,
    UNIQUE(account_id, principal_id, apply_idempotency_key, action_id)
);
CREATE INDEX idx_assistant_action_history_latest
ON assistant_action_history(account_id, principal_id, undone_at, id DESC);
