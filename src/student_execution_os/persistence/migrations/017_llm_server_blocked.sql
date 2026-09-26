-- Schema v17: a provider that refuses this server (HTTP 403 that does not blame the
-- key, e.g. Groq for a server in a region it does not serve) is its own status
-- instead of "the key is wrong". SQLite cannot widen a CHECK constraint in place, so
-- the table is rebuilt with identical columns.
BEGIN;

CREATE TABLE llm_credentials_v17 (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic', 'openai-compatible')),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    base_url TEXT,
    key_ciphertext BLOB NOT NULL,
    key_nonce BLOB NOT NULL,
    key_id TEXT NOT NULL,
    key_hint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNTESTED'
        CHECK (status IN ('UNTESTED', 'OK', 'INVALID_KEY', 'MODEL_NOT_FOUND', 'ENDPOINT_NOT_FOUND',
                          'RATE_LIMITED', 'QUOTA_EXCEEDED', 'UNSUPPORTED_FORMAT', 'MALFORMED_RESPONSE',
                          'REJECTED', 'UNREACHABLE', 'PROVIDER_ERROR', 'BLOCKED_URL', 'UNREADABLE',
                          'SERVER_BLOCKED')),
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
INSERT INTO llm_credentials_v17(account_id,provider,model,base_url,key_ciphertext,key_nonce,key_id,key_hint,status,
                                last_checked_at,created_at,updated_at,version)
    SELECT account_id,provider,model,base_url,key_ciphertext,key_nonce,key_id,key_hint,status,
           last_checked_at,created_at,updated_at,version FROM llm_credentials;
DROP TABLE llm_credentials;
ALTER TABLE llm_credentials_v17 RENAME TO llm_credentials;

COMMIT;
