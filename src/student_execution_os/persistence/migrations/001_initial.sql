PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    server_revision INTEGER NOT NULL DEFAULT 0 CHECK (server_revision >= 0)
);

CREATE TABLE IF NOT EXISTS obligations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('TASK','EVENT')),
    category TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('DRAFT','ACTIVE','COMPLETED','CANCELLED','ARCHIVED')),
    importance TEXT NOT NULL CHECK (importance IN ('LOW','NORMAL','HIGH','CRITICAL')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    UNIQUE(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_obligations_account ON obligations(account_id);

CREATE TABLE IF NOT EXISTS tasks (
    obligation_id TEXT PRIMARY KEY REFERENCES obligations(id) ON DELETE CASCADE,
    estimated_total_effort_minutes INTEGER NOT NULL CHECK (estimated_total_effort_minutes > 0),
    remaining_effort_minutes INTEGER NOT NULL CHECK (remaining_effort_minutes >= 0),
    splittable INTEGER NOT NULL CHECK (splittable IN (0,1)),
    min_chunk_minutes INTEGER CHECK (min_chunk_minutes IS NULL OR min_chunk_minutes > 0),
    max_chunk_minutes INTEGER CHECK (max_chunk_minutes IS NULL OR max_chunk_minutes > 0),
    actionable_from TEXT,
    cutoff_state TEXT NOT NULL CHECK (cutoff_state IN ('UNKNOWN','ABSENT','KNOWN')),
    actual_cutoff_at TEXT,
    cutoff_boundary TEXT CHECK (cutoff_boundary IS NULL OR cutoff_boundary IN ('INCLUSIVE','EXCLUSIVE')),
    cutoff_precision TEXT CHECK (cutoff_precision IS NULL OR cutoff_precision IN ('EXACT_INSTANT','DATE_ONLY','BOUNDED','UNKNOWN')),
    target_at TEXT,
    CHECK (max_chunk_minutes IS NULL OR min_chunk_minutes IS NULL OR max_chunk_minutes >= min_chunk_minutes),
    CHECK (
      (cutoff_state='KNOWN' AND actual_cutoff_at IS NOT NULL AND cutoff_boundary IS NOT NULL AND cutoff_precision='EXACT_INSTANT')
      OR
      (cutoff_state IN ('UNKNOWN','ABSENT') AND actual_cutoff_at IS NULL AND cutoff_boundary IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS events (
    obligation_id TEXT PRIMARY KEY REFERENCES obligations(id) ON DELETE CASCADE,
    time_semantics TEXT NOT NULL CHECK (time_semantics='FIXED_INTERVAL'),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    attendance_policy TEXT NOT NULL CHECK (attendance_policy IN ('REQUIRED','OPTIONAL','PREFERRED')),
    location_effect_kind TEXT NOT NULL CHECK (location_effect_kind IN ('NONE','REMOTE','STAY','MOVE')),
    origin_place_id TEXT,
    destination_place_id TEXT,
    CHECK (starts_at < ends_at)
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED','ARCHIVED')),
    importance TEXT CHECK (importance IS NULL OR importance IN ('LOW','NORMAL','HIGH','CRITICAL')),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_projects_account ON projects(account_id);

CREATE TABLE IF NOT EXISTS project_members (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    obligation_id TEXT NOT NULL,
    PRIMARY KEY(account_id, project_id, obligation_id),
    FOREIGN KEY(account_id, project_id) REFERENCES projects(account_id, id) ON DELETE CASCADE,
    FOREIGN KEY(account_id, obligation_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS milestones (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('OBLIGATION','PROJECT')),
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    marker_at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('INTERMEDIATE','PENALTY_START','FINAL_CUTOFF')),
    consequence TEXT,
    hard_for_planning INTEGER NOT NULL CHECK (hard_for_planning IN (0,1)),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED')),
    version INTEGER NOT NULL CHECK (version >= 1),
    CHECK (NOT (owner_kind='OBLIGATION' AND role='FINAL_CUTOFF')),
    UNIQUE(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_milestones_owner ON milestones(account_id, owner_kind, owner_id);

CREATE TABLE IF NOT EXISTS dependencies (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    predecessor_task_id TEXT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    successor_kind TEXT NOT NULL CHECK (successor_kind IN ('TASK','EVENT','MILESTONE')),
    successor_id TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'MUST_COMPLETE_BEFORE' CHECK (type='MUST_COMPLETE_BEFORE'),
    UNIQUE(account_id, predecessor_task_id, successor_kind, successor_id)
);
CREATE INDEX IF NOT EXISTS idx_dependencies_account ON dependencies(account_id);

CREATE TABLE IF NOT EXISTS user_time_constraints (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('FIXED_PERSONAL_BLOCK','UNAVAILABLE','PINNED_WORK')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    obligation_id TEXT,
    reason TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    CHECK (starts_at < ends_at),
    CHECK (type != 'PINNED_WORK' OR obligation_id IS NOT NULL),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, obligation_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_constraints_account ON user_time_constraints(account_id);

CREATE TABLE IF NOT EXISTS audit_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    server_revision INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_category TEXT NOT NULL CHECK (actor_category IN ('USER_UI','USER_VIA_LLM','CONNECTOR_INGESTION','RECONCILER','PLANNER','SYSTEM','ADMIN')),
    committed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(account_id, server_revision)
);
CREATE INDEX IF NOT EXISTS idx_audit_account_revision ON audit_changes(account_id, server_revision);
