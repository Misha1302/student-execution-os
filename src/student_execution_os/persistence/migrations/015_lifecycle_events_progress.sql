-- Schema v15: task lifecycle (archive / delete), fixed-time event reminders and
-- counted progress.

-- A task or event the user deleted. The row itself is removed (with every
-- cascading reference); this tombstone only answers late offline replays: an
-- operation for a deleted id is a harmless NOOP instead of an error, and a delayed
-- task.create/event.create with that id cannot bring the item back.
CREATE TABLE IF NOT EXISTS deleted_obligations (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    obligation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('TASK','EVENT')),
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, obligation_id)
);

-- "Remind me N minutes before" for a fixed-time event. The reminder moment itself
-- lives in reminder_states.remind_at (keyed by the event id) and is recomputed when
-- the event moves.
CREATE TABLE IF NOT EXISTS event_reminders (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    lead_minutes INTEGER NOT NULL CHECK (lead_minutes BETWEEN 0 AND 1440),
    PRIMARY KEY (account_id, event_id),
    FOREIGN KEY (account_id, event_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

-- Progress that is not time: "3 of 10 problems", "40 of 120 pages". Time remains the
-- planner's unit; counted progress lowers remaining effort proportionally.
CREATE TABLE IF NOT EXISTS task_progress_counts (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    total INTEGER NOT NULL CHECK (total BETWEEN 1 AND 100000),
    done INTEGER NOT NULL CHECK (done >= 0 AND done <= total),
    unit TEXT CHECK (unit IS NULL OR length(unit) <= 40),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, task_id),
    FOREIGN KEY (account_id, task_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
