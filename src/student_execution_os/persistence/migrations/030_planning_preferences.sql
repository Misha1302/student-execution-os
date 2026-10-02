CREATE TABLE planning_preferences (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('KEEP_FREE','WORK_LIMIT','AVOID_WORK','REST_AFTER_EVENTS')),
    anchor TEXT NOT NULL DEFAULT 'CLOCK' CHECK (anchor IN ('CLOCK','WAKE')),
    target TEXT NOT NULL DEFAULT 'ALL'
        CHECK (target IN ('ALL','DEMANDING','STUDY','CLASSES','ALL_EVENTS')),
    date_from TEXT NOT NULL,
    date_until TEXT,
    window_start TEXT,
    window_end TEXT,
    minutes INTEGER CHECK (minutes IS NULL OR (minutes >= 0 AND minutes <= 1440)),
    reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_planning_preferences_account ON planning_preferences(account_id, date_from);
