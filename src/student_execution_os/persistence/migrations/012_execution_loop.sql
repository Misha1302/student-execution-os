-- Schema v12: offline sync operations, task execution state and the reminder
-- execution loop. The v7-v11 revision-bound notification workflow is replaced:
-- it suppressed every pending reminder whenever any unrelated entity changed and
-- only ran while a client polled /today.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS notification_delivery_outbox;
DROP TABLE IF EXISTS notification_delivery_outbox_v10;
DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS notifications_v10;
DROP TABLE IF EXISTS notification_preferences;

PRAGMA foreign_keys = ON;

-- Execution facts owned by the user, not by the planner.
ALTER TABLE tasks ADD COLUMN started_at TEXT;
ALTER TABLE tasks ADD COLUMN last_progress_at TEXT;

-- Client operation log. One row per client-generated op_id makes every offline
-- mutation exactly-once from the client's point of view: a replayed op returns the
-- stored result instead of being applied twice. Written in the same transaction as
-- the mutation it records.
CREATE TABLE client_operations (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    op_id TEXT NOT NULL CHECK (length(op_id) BETWEEN 8 AND 128),
    op_type TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('APPLIED','NOOP','CONFLICT','REJECTED')),
    result_json TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, op_id)
);
CREATE INDEX client_operations_created ON client_operations(account_id, created_at);

CREATE TABLE reminder_preferences (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    intensity TEXT NOT NULL DEFAULT 'NORMAL' CHECK (intensity IN ('GENTLE','NORMAL','PERSISTENT')),
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    quiet_starts_local TEXT NOT NULL DEFAULT '22:00',
    quiet_ends_local TEXT NOT NULL DEFAULT '08:00',
    locale TEXT NOT NULL DEFAULT 'ru' CHECK (locale IN ('ru','en')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);

-- Per-task reminder episode. An episode is keyed by the task's deadline/target so a
-- reschedule starts a fresh escalation instead of inheriting "ignored" history.
CREATE TABLE reminder_states (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    episode_key TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    ignored_count INTEGER NOT NULL DEFAULT 0 CHECK (ignored_count >= 0),
    last_sent_at TEXT,
    last_stage TEXT,
    last_risk TEXT,
    stages_sent_json TEXT NOT NULL DEFAULT '[]',
    last_interaction_at TEXT,
    snoozed_until TEXT,
    closed_reason TEXT,
    next_check_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, task_id)
);

-- User-visible reminder messages (in-app inbox) and the durable push outbox.
-- Delivery retry (technical) lives in attempts/next_attempt_at; a new user reminder
-- is always a new row created by the reminder engine.
CREATE TABLE reminder_messages (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    dedupe_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    task_ids_json TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    deep_link TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery_state TEXT NOT NULL CHECK (delivery_state IN ('PENDING','LEASED','SENT','NO_DEVICE','CANCELLED','DEAD')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    provider_ids TEXT,
    seen_at TEXT,
    acted_at TEXT,
    acted_action TEXT,
    UNIQUE (account_id, dedupe_key)
);
CREATE INDEX reminder_messages_due ON reminder_messages(delivery_state, next_attempt_at);
CREATE INDEX reminder_messages_account ON reminder_messages(account_id, created_at);

-- Liveness of background processes, surfaced in health/diagnostics.
CREATE TABLE worker_heartbeats (
    name TEXT PRIMARY KEY,
    beat_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
