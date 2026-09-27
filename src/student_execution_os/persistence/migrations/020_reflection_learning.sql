-- Schema v20: explicit daily intent/reflection plus user-approved estimation hints.
-- Reflection observations are canonical user notes. Calibration measurements remain
-- derived from plan/execution history and are never written back into Task estimates.

CREATE TABLE IF NOT EXISTS daily_intents (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    focus_note TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, local_date)
);

CREATE TABLE IF NOT EXISTS daily_intent_tasks (
    account_id TEXT NOT NULL,
    local_date TEXT NOT NULL,
    task_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 4),
    PRIMARY KEY(account_id, local_date, position),
    UNIQUE(account_id, local_date, task_id),
    FOREIGN KEY(account_id, local_date) REFERENCES daily_intents(account_id, local_date) ON DELETE CASCADE,
    FOREIGN KEY(account_id, task_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_reflections (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    local_date TEXT NOT NULL,
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    summary TEXT,
    wins TEXT,
    blockers TEXT,
    adjustment TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, local_date)
);

CREATE TABLE IF NOT EXISTS weekly_reviews (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    week_starts_on TEXT NOT NULL,
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    summary TEXT,
    wins TEXT,
    blockers TEXT,
    adjustment TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, week_starts_on)
);

CREATE TABLE IF NOT EXISTS effort_calibration_preferences (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    multiplier REAL NOT NULL CHECK (multiplier BETWEEN 0.25 AND 4.0),
    based_on_samples INTEGER NOT NULL CHECK (based_on_samples >= 0),
    accepted_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);
