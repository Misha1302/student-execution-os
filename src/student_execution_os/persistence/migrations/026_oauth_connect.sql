-- Schema v26: OAuth 2.1 (authorization code + PKCE) for connecting ChatGPT/Codex/MCP
-- clients. The access token an exchange returns IS a v25 capability grant token, so the
-- grant stays the only credential, revocable in Settings. Codes are stored hashed and
-- are single use; clients are public (no secret) and registered dynamically.
CREATE TABLE oauth_clients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    redirect_uris_json TEXT NOT NULL CHECK (json_valid(redirect_uris_json) AND json_type(redirect_uris_json) = 'array'),
    created_at TEXT NOT NULL
);

CREATE TABLE oauth_authorizations (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_clients(id) ON DELETE CASCADE,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL CHECK (length(code_challenge) BETWEEN 43 AND 128),
    requested_scopes_json TEXT NOT NULL CHECK (json_valid(requested_scopes_json)),
    state TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','DENIED','EXCHANGED')),
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    approved_scopes_json TEXT CHECK (approved_scopes_json IS NULL OR json_valid(approved_scopes_json)),
    expires_in_days INTEGER,
    code_hash TEXT UNIQUE,
    grant_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK (status = 'PENDING' OR account_id IS NOT NULL)
);

CREATE INDEX idx_oauth_authorizations_account ON oauth_authorizations(account_id);
