-- Schema v23: recurrence exceptions and stable external identity (ADR 0027).
--
-- Occurrence overrides get a layer: SOURCE (what an imported timetable says) and USER
-- (what the student changed). The effective occurrence applies SOURCE, then USER on top;
-- "restore" removes only the USER layer. Overrides can now also change details (title,
-- room, teacher, note) without moving the occurrence.

ALTER TABLE recurring_templates ADD COLUMN location_text TEXT;
ALTER TABLE recurring_templates ADD COLUMN teacher TEXT;
-- Imported series: owned by a source; its template fields change only by source apply.
ALTER TABLE recurring_templates ADD COLUMN source_system_id TEXT;

CREATE TABLE occurrence_overrides_v23 (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    original_recurrence_id TEXT NOT NULL,
    layer TEXT NOT NULL DEFAULT 'USER' CHECK (layer IN ('USER','SOURCE')),
    action TEXT NOT NULL CHECK (action IN ('CANCEL','MODIFY')),
    replacement_start_local TEXT,
    replacement_duration_minutes INTEGER CHECK (replacement_duration_minutes IS NULL OR replacement_duration_minutes > 0),
    replacement_title TEXT CHECK (replacement_title IS NULL OR length(trim(replacement_title)) > 0),
    location_text TEXT,
    teacher TEXT,
    note TEXT,
    reason TEXT CHECK (reason IS NULL OR reason IN ('USER','HOLIDAY','SOURCE')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, template_id, original_recurrence_id, layer),
    FOREIGN KEY(account_id, template_id) REFERENCES recurring_templates(account_id, id) ON DELETE CASCADE,
    CHECK (
      (action='CANCEL' AND replacement_start_local IS NULL AND replacement_duration_minutes IS NULL
        AND replacement_title IS NULL AND location_text IS NULL AND teacher IS NULL)
      OR (action='MODIFY' AND (replacement_start_local IS NOT NULL OR replacement_duration_minutes IS NOT NULL
        OR replacement_title IS NOT NULL OR location_text IS NOT NULL OR teacher IS NOT NULL OR note IS NOT NULL))
    )
);
INSERT INTO occurrence_overrides_v23(id,account_id,template_id,original_recurrence_id,layer,action,
    replacement_start_local,replacement_duration_minutes,reason,version,created_at,updated_at)
SELECT id,account_id,template_id,original_recurrence_id,'USER',action,
    replacement_start_local,replacement_duration_minutes,'USER',version,created_at,updated_at
FROM occurrence_overrides;
DROP TABLE occurrence_overrides;
ALTER TABLE occurrence_overrides_v23 RENAME TO occurrence_overrides;
CREATE INDEX IF NOT EXISTS idx_occurrence_overrides_template
ON occurrence_overrides(account_id, template_id, original_recurrence_id);

-- An extra class of a series is an ordinary canonical event (Today, plan, reminders all
-- work) linked to its series for display and provenance.
CREATE TABLE IF NOT EXISTS series_extra_events (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    template_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, event_id),
    FOREIGN KEY(account_id, template_id) REFERENCES recurring_templates(account_id, id) ON DELETE CASCADE
);

-- Room and teacher of a one-off event (an extra class, an imported consultation).
CREATE TABLE IF NOT EXISTS event_details (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    location_text TEXT,
    teacher TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, event_id)
);

-- Stable external identity: (source, UID, RECURRENCE-ID) -> local entity. The master of a
-- series has external_recurrence_id ''. source_sequence/source_updated_at order updates, so
-- a late (stale) update of the same identity is ignored instead of overwriting newer data.
CREATE TABLE IF NOT EXISTS external_identities (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source_system_id TEXT NOT NULL,
    external_uid TEXT NOT NULL CHECK (length(external_uid) > 0),
    external_recurrence_id TEXT NOT NULL DEFAULT '',
    local_kind TEXT NOT NULL CHECK (local_kind IN ('SERIES','OCCURRENCE','EVENT')),
    local_id TEXT NOT NULL,
    template_id TEXT,
    original_recurrence_id TEXT,
    source_sequence INTEGER NOT NULL DEFAULT 0,
    source_updated_at TEXT,
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE','REMOVED')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(account_id, source_system_id, external_uid, external_recurrence_id)
);
CREATE INDEX IF NOT EXISTS idx_external_identities_local
ON external_identities(account_id, local_kind, local_id);
