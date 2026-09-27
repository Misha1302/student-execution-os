-- Schema v21: STARTER platform-managed AI usage and hard-cap reservations.
--
-- No prompt, response, provider credential, or other content is stored here. A
-- reservation charges the conservative maximum before an outbound request; after a
-- successful response it is reconciled to the provider-reported token counts.

CREATE TABLE IF NOT EXISTS starter_llm_account_usage (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    token_count INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, period_start)
);

CREATE TABLE IF NOT EXISTS starter_llm_global_usage (
    period_start TEXT PRIMARY KEY,
    period_end TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    token_count INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS starter_llm_reservations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens > 0),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    completion_tokens INTEGER CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
    status TEXT NOT NULL CHECK (status IN ('RESERVED','RECONCILED')),
    created_at TEXT NOT NULL,
    reconciled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_starter_llm_reservations_account_period
ON starter_llm_reservations(account_id, period_start);
