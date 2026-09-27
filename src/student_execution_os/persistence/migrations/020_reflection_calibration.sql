-- Schema v20: daily planning intent and explicit calibration preferences.
-- Review metrics remain derived from canonical tasks, plans and execution history.

CREATE TABLE IF NOT EXISTS daily_intents (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL CHECK (length(local_date)=10),
    priority_task_ids_json TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    started_at TEXT,
    closed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, local_date)
);

CREATE TABLE IF NOT EXISTS calibration_preferences (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    safety_multiplier REAL NOT NULL DEFAULT 1.0 CHECK (safety_multiplier BETWEEN 1.0 AND 3.0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    suppress_suggestion INTEGER NOT NULL DEFAULT 0 CHECK (suppress_suggestion IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, category)
);

CREATE INDEX IF NOT EXISTS idx_daily_intents_account_date
ON daily_intents(account_id, local_date);
