-- Schema v27: collaborative academic groups.
--
-- The group owns SHARED academic facts (its schedule items). Each active member receives
-- them as SOURCE state in their own account through SourceApplier (source system per
-- member+group); a member's personal changes stay in their USER layer, which the group
-- can neither read nor write. Group tables never reference personal execution state.
CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    timezone_name TEXT NOT NULL,
    schedule_revision INTEGER NOT NULL DEFAULT 0 CHECK (schedule_revision >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('OWNER','STAROSTA','MEMBER')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','LEFT','REMOVED')),
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, account_id)
);
CREATE INDEX idx_group_members_account ON group_members(account_id, status);

CREATE TABLE group_invitations (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE CHECK (length(code_hash) = 64),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_uses INTEGER NOT NULL CHECK (max_uses BETWEEN 1 AND 500),
    uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
    revoked_at TEXT
);

CREATE TABLE group_schedule_items (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    uid TEXT NOT NULL CHECK (length(uid) BETWEEN 8 AND 128),
    kind TEXT NOT NULL CHECK (kind IN ('SERIES','EVENT')),
    item_json TEXT NOT NULL CHECK (json_valid(item_json)),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    PRIMARY KEY (group_id, uid)
);

CREATE TABLE group_proposals (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    proposer_account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (action IN ('UPSERT','REMOVE')),
    uid TEXT NOT NULL,
    kind TEXT CHECK (kind IS NULL OR kind IN ('SERIES','EVENT')),
    item_json TEXT CHECK (item_json IS NULL OR json_valid(item_json)),
    note TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','WITHDRAWN')),
    decided_by TEXT,
    decided_at TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_group_proposals_group ON group_proposals(group_id, status, created_at);
