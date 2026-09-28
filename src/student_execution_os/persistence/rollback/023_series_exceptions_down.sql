-- Roll schema v23 back to v22 (ADR 0027). Fail before changing persistent schema when
-- v23 contains state that v22 cannot represent. Export/remove that state explicitly first.
CREATE TEMP TABLE _v23_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER _v23_rollback_guard_check
BEFORE INSERT ON _v23_rollback_guard
WHEN EXISTS (SELECT 1 FROM occurrence_overrides WHERE layer='SOURCE'
             OR replacement_title IS NOT NULL OR location_text IS NOT NULL
             OR teacher IS NOT NULL OR note IS NOT NULL
             OR (action='MODIFY' AND replacement_start_local IS NULL))
  OR EXISTS (SELECT 1 FROM recurring_templates WHERE source_system_id IS NOT NULL)
  OR EXISTS (SELECT 1 FROM series_extra_events)
  OR EXISTS (SELECT 1 FROM event_details)
  OR EXISTS (SELECT 1 FROM external_identities)
BEGIN
  SELECT RAISE(ABORT, 'v23 rollback would discard source/import/detail state; export or remove it first');
END;
INSERT INTO _v23_rollback_guard VALUES (1);
DROP TRIGGER _v23_rollback_guard_check;
DROP TABLE _v23_rollback_guard;

-- Every remaining override has a lossless v22 representation.
DROP TABLE IF EXISTS external_identities;
DROP TABLE IF EXISTS series_extra_events;
DROP TABLE IF EXISTS event_details;

CREATE TABLE occurrence_overrides_v22 (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    original_recurrence_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('CANCEL','MODIFY')),
    replacement_start_local TEXT,
    replacement_duration_minutes INTEGER CHECK (replacement_duration_minutes IS NULL OR replacement_duration_minutes > 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, template_id, original_recurrence_id),
    FOREIGN KEY(account_id, template_id) REFERENCES recurring_templates(account_id, id) ON DELETE CASCADE,
    CHECK (
      (action='CANCEL' AND replacement_start_local IS NULL AND replacement_duration_minutes IS NULL)
      OR (action='MODIFY' AND replacement_start_local IS NOT NULL)
    )
);
INSERT INTO occurrence_overrides_v22(id,account_id,template_id,original_recurrence_id,action,
    replacement_start_local,replacement_duration_minutes,version,created_at,updated_at)
SELECT id,account_id,template_id,original_recurrence_id,action,
    replacement_start_local,replacement_duration_minutes,version,created_at,updated_at
FROM occurrence_overrides
WHERE layer='USER' AND (action='CANCEL' OR replacement_start_local IS NOT NULL);
DROP TABLE occurrence_overrides;
ALTER TABLE occurrence_overrides_v22 RENAME TO occurrence_overrides;
CREATE INDEX IF NOT EXISTS idx_occurrence_overrides_template
ON occurrence_overrides(account_id, template_id, original_recurrence_id);

ALTER TABLE recurring_templates DROP COLUMN source_system_id;
ALTER TABLE recurring_templates DROP COLUMN teacher;
ALTER TABLE recurring_templates DROP COLUMN location_text;
DELETE FROM schema_migrations WHERE version=23;
