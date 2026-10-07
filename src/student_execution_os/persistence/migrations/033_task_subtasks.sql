-- Schema v33: checklists inside a Task (ADR 0036).
--
-- A subtask is a step of one Task, not a Task: the planner never sees it, and the
-- Task's remaining effort stays the only planning effort (no double counting). A
-- subtask's own effort is a breakdown hint, compared with the Task's estimate but never
-- summed into the plan. Order is a fractional position so two offline devices can
-- insert and move items without renumbering everything.
CREATE TABLE IF NOT EXISTS task_subtasks (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
    position REAL NOT NULL,
    effort_minutes INTEGER CHECK (effort_minutes IS NULL OR effort_minutes BETWEEN 1 AND 100000),
    done_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, task_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_subtasks_task ON task_subtasks(account_id, task_id, position, id);
