-- Schema v25: least-authority capability grants for external agents (MCP, ChatGPT, Codex).
--
-- A grant is a bearer credential scoped to one account. Only a SHA-256 of its secret is
-- stored; the plaintext token is shown once at creation. Grants cannot mint grants.
-- Every mutation still goes through /api/v1/sync semantics (client_operations op_id log).
CREATE TABLE capability_grants (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 80),
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json) AND json_type(scopes_json) = 'array'),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE INDEX idx_capability_grants_account ON capability_grants(account_id, created_at);
