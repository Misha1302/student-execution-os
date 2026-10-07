-- Schema v31: tracked check-ins and recurring reminders (ADR 0034).
--
-- A check-in is a small repeated action whose actual outcome matters ("принять
-- сертралин в 9", "20 задач матана в день"). It is not a Task: it is never planned
-- as work and never consumes planner capacity by itself. The occurrence row is the
-- one owner of what really happened; a reminder (reminders table) only draws
-- attention to it, so snoozing changes the reminder, never the outcome.
--
-- A reminder series ("каждый день в 22:30 вынести мусор") is attention only: it
-- materializes ordinary standalone reminders, one per occurrence, with a stable
-- (series_id, original_recurrence_id) identity.

CREATE TABLE IF NOT EXISTS checkin_templates (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('ROUTINE','MEDICATION','QUOTA')),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
    -- MEDICATION: the user's own words, never interpreted (no pharmacology here).
    dose_text TEXT CHECK (dose_text IS NULL OR length(dose_text) <= 200),
    instructions TEXT CHECK (instructions IS NULL OR length(instructions) <= 1000),
    -- QUOTA: "20 задач в день"; effort per unit is optional and only ever user-given.
    target_quantity INTEGER CHECK (target_quantity IS NULL OR target_quantity BETWEEN 1 AND 100000),
    unit TEXT CHECK (unit IS NULL OR length(unit) <= 40),
    unit_effort_seconds INTEGER CHECK (unit_effort_seconds IS NULL OR unit_effort_seconds BETWEEN 1 AND 86400),
    dtstart_local TEXT NOT NULL,
    recurrence_rule TEXT NOT NULL CHECK (length(trim(recurrence_rule)) > 0),
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    -- Prompt policy (delivery), kept apart from the outcome columns of occurrences.
    remind INTEGER NOT NULL DEFAULT 1 CHECK (remind IN (0,1)),
    delivery TEXT NOT NULL DEFAULT 'PUSH' CHECK (delivery IN ('PUSH','ALARM','PUSH_AND_ALARM')),
    followup_minutes INTEGER CHECK (followup_minutes IS NULL OR followup_minutes BETWEEN 5 AND 720),
    -- Observation window: after it an unanswered occurrence is recorded MISSED.
    -- NULL = until the end of the local day or the next occurrence, whichever is first.
    window_minutes INTEGER CHECK (window_minutes IS NULL OR window_minutes BETWEEN 5 AND 1440),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ENDED')),
    series_end_before_local TEXT,
    actor_category TEXT NOT NULL DEFAULT 'USER_UI',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, id),
    CHECK ((kind = 'QUOTA') = (target_quantity IS NOT NULL)),
    CHECK (kind = 'QUOTA' OR (unit IS NULL AND unit_effort_seconds IS NULL)),
    CHECK (kind = 'MEDICATION' OR (dose_text IS NULL AND instructions IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_checkin_templates_account ON checkin_templates(account_id, status, id);

CREATE TABLE IF NOT EXISTS checkin_occurrences (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    original_recurrence_id TEXT NOT NULL,
    -- One occurrence moved ("перенеси только сегодняшний приём на 22:00"): local civil time.
    moved_to_local TEXT,
    -- Outcome (the source of truth about what happened).
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','DONE','SKIPPED','MISSED','CANCELLED')),
    resolved_by TEXT CHECK (resolved_by IS NULL OR resolved_by IN ('USER','POLICY')),
    occurred_at TEXT,
    acted_at TEXT,
    quantity_done INTEGER NOT NULL DEFAULT 0 CHECK (quantity_done BETWEEN 0 AND 1000000),
    -- QUOTA: the target as it was for this day, so a later change keeps history honest.
    target_quantity INTEGER CHECK (target_quantity IS NULL OR target_quantity BETWEEN 1 AND 100000),
    note TEXT CHECK (note IS NULL OR length(note) <= 500),
    -- Delivery bookkeeping (not an outcome): the reminder that draws attention to this
    -- occurrence and how many policy follow-ups were issued.
    reminder_id TEXT REFERENCES reminders(id) ON DELETE SET NULL,
    followups_sent INTEGER NOT NULL DEFAULT 0 CHECK (followups_sent >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, template_id, original_recurrence_id),
    FOREIGN KEY (account_id, template_id) REFERENCES checkin_templates(account_id, id) ON DELETE CASCADE,
    CHECK ((status = 'PENDING') = (resolved_by IS NULL)),
    CHECK (resolved_by IS NULL OR acted_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_checkin_occurrences_open ON checkin_occurrences(account_id, status, original_recurrence_id);
CREATE INDEX IF NOT EXISTS idx_checkin_occurrences_reminder ON checkin_occurrences(reminder_id);

CREATE TABLE IF NOT EXISTS reminder_series (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    dtstart_local TEXT NOT NULL,
    recurrence_rule TEXT NOT NULL CHECK (length(trim(recurrence_rule)) > 0),
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    delivery TEXT NOT NULL DEFAULT 'PUSH' CHECK (delivery IN ('PUSH','ALARM','PUSH_AND_ALARM')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ENDED')),
    series_end_before_local TEXT,
    actor_category TEXT NOT NULL DEFAULT 'USER_UI',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_reminder_series_account ON reminder_series(account_id, status, id);

-- Which standalone reminder is which occurrence of a series. The reminder id is
-- derived from (account, series, original recurrence id), so a deleted occurrence
-- stays deleted (deleted_reminders) instead of being materialized again.
CREATE TABLE IF NOT EXISTS reminder_series_occurrences (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    series_id TEXT NOT NULL,
    original_recurrence_id TEXT NOT NULL,
    reminder_id TEXT NOT NULL UNIQUE REFERENCES reminders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, series_id, original_recurrence_id),
    FOREIGN KEY (account_id, series_id) REFERENCES reminder_series(account_id, id) ON DELETE CASCADE
);

-- Delete tombstones of the entity kinds added from v31 on (CHECKIN, REMINDER_SERIES,
-- ...): a late offline replay for a deleted item is a NOOP and cannot resurrect it.
CREATE TABLE IF NOT EXISTS deleted_entities (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    entity_kind TEXT NOT NULL CHECK (length(entity_kind) BETWEEN 1 AND 40),
    entity_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, entity_kind, entity_id)
);
