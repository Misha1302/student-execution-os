-- Schema v19: recurring work templates and stable materialized occurrences.
-- Calendar recurrence remains event-only. Recurring work materializes canonical Task
-- obligations so planner/execution/progress keep one Task ownership model.

CREATE TABLE IF NOT EXISTS work_routine_templates (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT,
    category TEXT NOT NULL,
    importance TEXT NOT NULL CHECK (importance IN ('LOW','NORMAL','HIGH','CRITICAL')),
    dtstart_local TEXT NOT NULL,
    effort_minutes INTEGER NOT NULL CHECK (effort_minutes > 0),
    recurrence_rule TEXT NOT NULL CHECK (length(trim(recurrence_rule)) > 0),
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    splittable INTEGER NOT NULL CHECK (splittable IN (0,1)),
    min_chunk_minutes INTEGER CHECK (min_chunk_minutes IS NULL OR min_chunk_minutes > 0),
    max_chunk_minutes INTEGER CHECK (max_chunk_minutes IS NULL OR max_chunk_minutes > 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CANCELLED')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id,id),
    CHECK (max_chunk_minutes IS NULL OR min_chunk_minutes IS NULL OR max_chunk_minutes >= min_chunk_minutes)
);
CREATE INDEX IF NOT EXISTS idx_work_routines_account
ON work_routine_templates(account_id,status,id);

CREATE TABLE IF NOT EXISTS work_routine_occurrences (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    original_recurrence_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','SKIPPED')),
    override_title TEXT,
    override_effort_minutes INTEGER CHECK (override_effort_minutes IS NULL OR override_effort_minutes > 0),
    override_target_local TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id,template_id,original_recurrence_id),
    UNIQUE(account_id,task_id),
    FOREIGN KEY(account_id,template_id) REFERENCES work_routine_templates(account_id,id) ON DELETE CASCADE,
    FOREIGN KEY(account_id,task_id) REFERENCES obligations(account_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_work_routine_occurrence_task
ON work_routine_occurrences(account_id,task_id);
