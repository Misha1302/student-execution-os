-- Schema v14: per-account LLM access.
--
-- The default product model is "bring your own key": every account may store its own
-- provider credential. The key is encrypted with AES-256-GCM under a server master key
-- that lives outside the database (SEOS_CREDENTIAL_KEY_FILE); the database, its
-- backups and account exports never hold it in plaintext. The ciphertext is bound to
-- its account through the AEAD associated data, so a row copied to another account
-- does not decrypt.
CREATE TABLE IF NOT EXISTS llm_credentials (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic', 'openai-compatible')),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    base_url TEXT,
    key_ciphertext BLOB NOT NULL,
    key_nonce BLOB NOT NULL,
    key_id TEXT NOT NULL,
    key_hint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNTESTED'
        CHECK (status IN ('UNTESTED', 'OK', 'INVALID_KEY', 'MODEL_NOT_FOUND', 'RATE_LIMITED',
                          'REJECTED', 'UNREACHABLE', 'BLOCKED_URL', 'UNREADABLE')),
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

-- Seam for a future paid tier: an account entitled to PLATFORM_MANAGED access uses
-- the operator's credentials (SEOS_PLATFORM_LLM_*) without entering a key. Granting
-- is an operator/billing action; nothing grants it automatically yet.
CREATE TABLE IF NOT EXISTS llm_entitlements (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('PLATFORM_MANAGED')),
    plan TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT
);
