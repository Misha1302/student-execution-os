-- Roll schema v23 back to v22 (ADR 0027). Run ONLY after checking the preconditions in
-- the ADR: no SOURCE-layer overrides, no detail-only overrides, no imported series, no extra
-- classes and no external identities — otherwise export that data first. This script
-- keeps every USER override that the v22 shape can represent and nothing else.
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
