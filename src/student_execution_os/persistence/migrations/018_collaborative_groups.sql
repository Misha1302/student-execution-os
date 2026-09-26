-- Schema v18: collaborative (academic) groups — ADR 0021.
--
-- A group publishes facts ("15 Oct 12:10 control work"); every member keeps a
-- private overlay deciding what to do about them. The two layers never share a
-- table:
--
--   group-owned   groups, group_memberships, group_invites, shared_events,
--                 shared_obligations, shared_announcements, group_proposals,
--                 external_event_bindings, group_audit
--   user-owned    user_shared_event_states, user_shared_obligation_states,
--                 user_announcement_states, user_group_preferences
--
-- Group-owned rows are not account-scoped (a group spans accounts). Authors are
-- kept as plain ids without a foreign key so a member deleting their account does
-- not delete facts the rest of the group relies on (the lifecycle pseudonymises
-- them instead). Settings, join policy and proposal payloads are typed: columns
-- with CHECK constraints, and a proposal payload that must be valid JSON tagged
-- with its own kind (the domain layer validates the fields before every write).
BEGIN;

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 128),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    description TEXT CHECK (description IS NULL OR length(description) <= 2000),
    type TEXT NOT NULL CHECK (type IN ('ACADEMIC','PROJECT','OTHER')),
    owner_account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ARCHIVED','DELETED')),
    -- GroupSettings
    allow_members_to_publish INTEGER NOT NULL DEFAULT 0 CHECK (allow_members_to_publish IN (0,1)),
    default_timezone TEXT NOT NULL DEFAULT 'UTC' CHECK (length(default_timezone) BETWEEN 1 AND 64),
    default_show_regular_classes INTEGER NOT NULL DEFAULT 1 CHECK (default_show_regular_classes IN (0,1)),
    default_show_assessments INTEGER NOT NULL DEFAULT 1 CHECK (default_show_assessments IN (0,1)),
    default_show_deadlines INTEGER NOT NULL DEFAULT 1 CHECK (default_show_deadlines IN (0,1)),
    default_show_announcements INTEGER NOT NULL DEFAULT 1 CHECK (default_show_announcements IN (0,1)),
    -- GroupJoinPolicy
    invite_links_enabled INTEGER NOT NULL DEFAULT 1 CHECK (invite_links_enabled IN (0,1)),
    join_codes_enabled INTEGER NOT NULL DEFAULT 1 CHECK (join_codes_enabled IN (0,1)),
    approval_required INTEGER NOT NULL DEFAULT 0 CHECK (approval_required IN (0,1)),
    allow_rejoin_after_removal INTEGER NOT NULL DEFAULT 0 CHECK (allow_rejoin_after_removal IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS group_memberships (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('OWNER','ADMIN','SCHEDULER','MEMBER')),
    status TEXT NOT NULL CHECK (status IN ('PENDING','ACTIVE','LEFT','REMOVED','BLOCKED')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_group_memberships_account ON group_memberships(account_id, status);
-- Exactly one owner among the active members.
CREATE UNIQUE INDEX IF NOT EXISTS uq_group_active_owner
    ON group_memberships(group_id) WHERE role='OWNER' AND status='ACTIVE';

-- Invite links store only the SHA-256 of their random token (shown once, when
-- created). Join codes are meant to be read aloud, so the normalized code is kept
-- (its hash is still the lookup key) and admins can show it again.
CREATE TABLE IF NOT EXISTS group_invites (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('LINK','CODE')),
    token_hash TEXT NOT NULL UNIQUE,
    code TEXT,
    token_hint TEXT NOT NULL,
    created_by_account_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    max_uses INTEGER CHECK (max_uses IS NULL OR max_uses BETWEEN 1 AND 10000),
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','REVOKED')),
    revoked_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CHECK ((kind = 'CODE') = (code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_group_invites_group ON group_invites(group_id, status);

-- One canonical row per group fact; members never get a copy. An assessment is a
-- shared event whose kind is QUIZ/TEST/CONTROL_WORK/COLLOQUIUM/EXAM.
CREATE TABLE IF NOT EXISTS shared_events (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 128),
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    description TEXT CHECK (description IS NULL OR length(description) <= 5000),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('CLASS','LECTURE','SEMINAR','PRACTICE','LAB','CONSULTATION',
        'QUIZ','TEST','CONTROL_WORK','COLLOQUIUM','EXAM','GROUP_MEETING','OTHER')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    location TEXT CHECK (location IS NULL OR length(location) <= 200),
    source TEXT NOT NULL CHECK (source IN ('GROUP_MANUAL','EXTERNAL_ANNOTATION')),
    external_binding_id TEXT,
    attendance_default TEXT NOT NULL CHECK (attendance_default IN ('REQUIRED','PREFERRED','OPTIONAL','SKIP')),
    group_criticality TEXT NOT NULL CHECK (group_criticality IN ('NORMAL','IMPORTANT','CRITICAL')),
    author_account_id TEXT NOT NULL,
    proposal_id TEXT UNIQUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED','CANCELLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT,
    CHECK (starts_at < ends_at)
);
CREATE INDEX IF NOT EXISTS idx_shared_events_group ON shared_events(group_id, starts_at, id);

-- A result due by a moment without its own slot. There is no shared COMPLETED:
-- doing it belongs to each member's personal layer.
CREATE TABLE IF NOT EXISTS shared_obligations (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 128),
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    description TEXT CHECK (description IS NULL OR length(description) <= 5000),
    kind TEXT NOT NULL CHECK (kind IN ('ASSIGNMENT','LAB_REPORT','HOMEWORK','REGISTRATION','SUBMISSION','OTHER')),
    deadline TEXT NOT NULL,
    timezone TEXT NOT NULL CHECK (length(timezone) BETWEEN 1 AND 64),
    group_criticality TEXT NOT NULL CHECK (group_criticality IN ('NORMAL','IMPORTANT','CRITICAL')),
    estimated_effort_hint_minutes INTEGER CHECK (estimated_effort_hint_minutes IS NULL OR estimated_effort_hint_minutes BETWEEN 1 AND 100000),
    author_account_id TEXT NOT NULL,
    proposal_id TEXT UNIQUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED','CANCELLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_shared_obligations_group ON shared_obligations(group_id, deadline, id);

-- Information without a time of its own: never an Event or a Task.
CREATE TABLE IF NOT EXISTS shared_announcements (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 128),
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 5000),
    importance TEXT NOT NULL CHECK (importance IN ('NORMAL','IMPORTANT','URGENT')),
    author_account_id TEXT NOT NULL,
    proposal_id TEXT UNIQUE,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED','RETRACTED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_shared_announcements_group ON shared_announcements(group_id, created_at, id);

-- A member's suggestion. It is not a shared entity in a PROPOSED state: until it is
-- approved nothing is published. Approval writes approved_entity_* in the same
-- transaction that creates exactly one entity (whose proposal_id is UNIQUE).
CREATE TABLE IF NOT EXISTS group_proposals (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 8 AND 128),
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    proposal_kind TEXT NOT NULL CHECK (proposal_kind IN ('SHARED_EVENT','SHARED_OBLIGATION','SHARED_ANNOUNCEMENT')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_extract(payload_json, '$.kind') = proposal_kind),
    payload_hash TEXT NOT NULL,
    author_account_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','WITHDRAWN')),
    reviewer_account_id TEXT,
    review_comment TEXT CHECK (review_comment IS NULL OR length(review_comment) <= 1000),
    approved_entity_kind TEXT CHECK (approved_entity_kind IS NULL OR approved_entity_kind IN ('SHARED_EVENT','SHARED_OBLIGATION','SHARED_ANNOUNCEMENT')),
    approved_entity_id TEXT UNIQUE,
    approved_payload_json TEXT CHECK (approved_payload_json IS NULL OR json_valid(approved_payload_json)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((status = 'APPROVED') = (approved_entity_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_group_proposals_group ON group_proposals(group_id, status, created_at, id);
-- The same suggestion twice while the first is still pending is a duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_group_proposals_pending_payload
    ON group_proposals(group_id, payload_hash) WHERE status='PENDING';

-- Explicit relation between a shared annotation and an external event (Google,
-- ICS, a university schedule …). The source key names the external calendar the
-- same way for every member (connector provider + scope); the uid is the source's
-- stable event id and the occurrence key pins one occurrence of a recurring series.
-- The official_* columns are the external source's values last observed by the
-- member who bound it: a projection of external-owned data, never edited by the
-- group.
CREATE TABLE IF NOT EXISTS external_event_bindings (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    shared_event_id TEXT NOT NULL REFERENCES shared_events(id) ON DELETE CASCADE,
    external_source_key TEXT NOT NULL CHECK (length(external_source_key) BETWEEN 1 AND 300),
    external_event_uid TEXT NOT NULL CHECK (length(external_event_uid) BETWEEN 1 AND 500),
    external_occurrence_key TEXT CHECK (external_occurrence_key IS NULL OR length(external_occurrence_key) <= 500),
    relation_kind TEXT NOT NULL CHECK (relation_kind IN ('ANNOTATES')),
    bound_by_account_id TEXT NOT NULL,
    official_title TEXT,
    official_starts_at TEXT NOT NULL,
    official_ends_at TEXT NOT NULL,
    official_location TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','DETACHED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_binding_active_event
    ON external_event_bindings(shared_event_id) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_external_binding_identity
    ON external_event_bindings(group_id, external_source_key, external_event_uid, external_occurrence_key, status);

-- Who changed what, when, from which version to which, under which mutation.
CREATE TABLE IF NOT EXISTS group_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    entity_kind TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_account_id TEXT NOT NULL,
    at TEXT NOT NULL,
    previous_version INTEGER,
    new_version INTEGER,
    mutation_id TEXT,
    proposal_id TEXT,
    binding_id TEXT,
    changes_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(changes_json))
);
CREATE INDEX IF NOT EXISTS idx_group_audit_entity ON group_audit(group_id, entity_id, id);
CREATE INDEX IF NOT EXISTS idx_group_audit_actor ON group_audit(actor_account_id, action, at);

-- ---- user-owned overlays --------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_group_preferences (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0,1)),
    show_regular_classes INTEGER NOT NULL DEFAULT 1 CHECK (show_regular_classes IN (0,1)),
    show_assessments INTEGER NOT NULL DEFAULT 1 CHECK (show_assessments IN (0,1)),
    show_deadlines INTEGER NOT NULL DEFAULT 1 CHECK (show_deadlines IN (0,1)),
    show_announcements INTEGER NOT NULL DEFAULT 1 CHECK (show_announcements IN (0,1)),
    announcements_in_agenda INTEGER NOT NULL DEFAULT 0 CHECK (announcements_in_agenda IN (0,1)),
    default_attendance_behavior TEXT CHECK (default_attendance_behavior IS NULL OR
        default_attendance_behavior IN ('REQUIRED','PREFERRED','OPTIONAL','SKIP')),
    notification_behavior TEXT NOT NULL DEFAULT 'CHANGES'
        CHECK (notification_behavior IN ('SILENT','CHANGES','CHANGES_AND_CRITICAL')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, group_id)
);

CREATE TABLE IF NOT EXISTS user_shared_event_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    shared_event_id TEXT NOT NULL REFERENCES shared_events(id) ON DELETE CASCADE,
    attendance_override TEXT CHECK (attendance_override IS NULL OR attendance_override IN ('REQUIRED','PREFERRED','OPTIONAL','SKIP')),
    criticality_override TEXT CHECK (criticality_override IS NULL OR criticality_override IN ('NORMAL','IMPORTANT','CRITICAL')),
    remind_before_minutes INTEGER CHECK (remind_before_minutes IS NULL OR remind_before_minutes BETWEEN 0 AND 10080),
    alarm_before_minutes INTEGER CHECK (alarm_before_minutes IS NULL OR alarm_before_minutes BETWEEN 0 AND 10080),
    alarm_reminder_id TEXT,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0,1)),
    preparation_task_id TEXT,
    preparation_deadline_stale INTEGER NOT NULL DEFAULT 0 CHECK (preparation_deadline_stale IN (0,1)),
    last_seen_version INTEGER NOT NULL DEFAULT 0 CHECK (last_seen_version >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, shared_event_id)
);

CREATE TABLE IF NOT EXISTS user_shared_obligation_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    shared_obligation_id TEXT NOT NULL REFERENCES shared_obligations(id) ON DELETE CASCADE,
    acceptance_state TEXT NOT NULL DEFAULT 'UNDECIDED' CHECK (acceptance_state IN ('UNDECIDED','ACCEPTED','DECLINED')),
    personal_task_id TEXT,
    personal_deadline_stale INTEGER NOT NULL DEFAULT 0 CHECK (personal_deadline_stale IN (0,1)),
    criticality_override TEXT CHECK (criticality_override IS NULL OR criticality_override IN ('NORMAL','IMPORTANT','CRITICAL')),
    remind_before_minutes INTEGER CHECK (remind_before_minutes IS NULL OR remind_before_minutes BETWEEN 0 AND 10080),
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0,1)),
    last_seen_version INTEGER NOT NULL DEFAULT 0 CHECK (last_seen_version >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, shared_obligation_id)
);

CREATE TABLE IF NOT EXISTS user_announcement_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    announcement_id TEXT NOT NULL REFERENCES shared_announcements(id) ON DELETE CASCADE,
    dismissed INTEGER NOT NULL DEFAULT 0 CHECK (dismissed IN (0,1)),
    last_seen_version INTEGER NOT NULL DEFAULT 0 CHECK (last_seen_version >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, announcement_id)
);

COMMIT;
