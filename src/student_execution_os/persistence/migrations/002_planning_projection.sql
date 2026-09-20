ALTER TABLE tasks ADD COLUMN estimated_total_effort_low_minutes INTEGER CHECK (estimated_total_effort_low_minutes IS NULL OR estimated_total_effort_low_minutes > 0);
ALTER TABLE tasks ADD COLUMN estimated_total_effort_high_minutes INTEGER CHECK (estimated_total_effort_high_minutes IS NULL OR estimated_total_effort_high_minutes > 0);
ALTER TABLE tasks ADD COLUMN remaining_effort_low_minutes INTEGER CHECK (remaining_effort_low_minutes IS NULL OR remaining_effort_low_minutes >= 0);
ALTER TABLE tasks ADD COLUMN remaining_effort_high_minutes INTEGER CHECK (remaining_effort_high_minutes IS NULL OR remaining_effort_high_minutes >= 0);

CREATE TABLE IF NOT EXISTS plan_snapshots (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan_revision TEXT NOT NULL,
    input_server_revision INTEGER NOT NULL CHECK (input_server_revision >= 0),
    input_hash TEXT NOT NULL,
    horizon_start TEXT NOT NULL,
    horizon_end TEXT NOT NULL,
    feasibility_status TEXT NOT NULL CHECK (feasibility_status IN ('FEASIBLE','INFEASIBLE','UNKNOWN')),
    generated_at TEXT NOT NULL,
    explanations_json TEXT NOT NULL,
    UNIQUE(account_id, plan_revision)
);
CREATE INDEX IF NOT EXISTS idx_plan_snapshots_account ON plan_snapshots(account_id, generated_at);

CREATE TABLE IF NOT EXISTS plan_blocks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plan_snapshots(id) ON DELETE CASCADE,
    block_type TEXT NOT NULL CHECK (block_type IN ('WORK','EVENT_PROJECTION')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    obligation_id TEXT,
    source_constraint_ids_json TEXT NOT NULL,
    source_event_id TEXT,
    explanation TEXT NOT NULL,
    CHECK (starts_at < ends_at)
);
CREATE INDEX IF NOT EXISTS idx_plan_blocks_plan ON plan_blocks(plan_id, starts_at);

CREATE TABLE IF NOT EXISTS current_plans (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    plan_id TEXT NOT NULL REFERENCES plan_snapshots(id) ON DELETE CASCADE
);
