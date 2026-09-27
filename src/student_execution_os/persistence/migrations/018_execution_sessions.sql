-- Schema v18: canonical execution sessions and pause/resume segments.
-- Planned work remains derived. These rows record what the user actually did.

CREATE TABLE IF NOT EXISTS execution_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','PAUSED','FINISHED','CANCELLED')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    planning_snapshot_id TEXT,
    source_plan_block_id TEXT,
    remaining_effort_at_start INTEGER,
    estimated_total_effort_at_start INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, task_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_one_active_account
ON execution_sessions(account_id)
WHERE state IN ('ACTIVE','PAUSED');

CREATE INDEX IF NOT EXISTS idx_execution_task_history
ON execution_sessions(account_id, task_id, started_at DESC);

CREATE TABLE IF NOT EXISTS execution_segments (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, session_id)
        REFERENCES execution_sessions(account_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_one_open_segment
ON execution_segments(account_id, session_id)
WHERE ended_at IS NULL;
