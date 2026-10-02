CREATE TABLE auth_rate_limits (
    scope TEXT NOT NULL CHECK (scope IN ('LOGIN', 'IP')),
    key_hash TEXT NOT NULL CHECK (length(key_hash) = 64),
    window_started_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL CHECK (attempt_count > 0),
    PRIMARY KEY (scope, key_hash)
);
CREATE INDEX idx_auth_rate_limits_expiry ON auth_rate_limits(scope, window_started_at);
