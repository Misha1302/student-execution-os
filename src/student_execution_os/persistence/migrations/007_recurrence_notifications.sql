CREATE TABLE IF NOT EXISTS recurring_templates (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT,
    category TEXT NOT NULL,
    importance TEXT NOT NULL CHECK (importance IN ('LOW','NORMAL','HIGH','CRITICAL')),
    dtstart_local TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
    recurrence_rule TEXT NOT NULL CHECK (length(trim(recurrence_rule)) > 0),
    timezone_name TEXT NOT NULL CHECK (length(trim(timezone_name)) > 0),
    attendance_policy TEXT NOT NULL CHECK (attendance_policy IN ('REQUIRED','OPTIONAL','PREFERRED')),
    location_effect_kind TEXT NOT NULL CHECK (location_effect_kind IN ('NONE','REMOTE','STAY','MOVE')),
    origin_place_id TEXT,
    destination_place_id TEXT,
    arrival_requirement_minutes INTEGER NOT NULL DEFAULT 0 CHECK (arrival_requirement_minutes >= 0),
    resolution_policy TEXT NOT NULL CHECK (resolution_policy IN ('EARLIER_FOLD_SHIFT_FORWARD')),
    series_end_before_local TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, origin_place_id) REFERENCES places(account_id, id),
    FOREIGN KEY(account_id, destination_place_id) REFERENCES places(account_id, id),
    CHECK (
      (location_effect_kind IN ('NONE','REMOTE') AND origin_place_id IS NULL AND destination_place_id IS NULL)
      OR (location_effect_kind='STAY' AND origin_place_id IS NULL AND destination_place_id IS NOT NULL)
      OR (location_effect_kind='MOVE' AND origin_place_id IS NOT NULL AND destination_place_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_recurring_templates_account
ON recurring_templates(account_id, id);

CREATE TABLE IF NOT EXISTS occurrence_overrides (
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
CREATE INDEX IF NOT EXISTS idx_occurrence_overrides_template
ON occurrence_overrides(account_id, template_id, original_recurrence_id);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    suppression_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
      'LATEST_SAFE_DEPARTURE','LATEST_SAFE_START','RISK_THRESHOLD','DEADLINE_WARNING',
      'PLAN_CONFLICT','SOURCE_CHANGE','COMPLETION_FOLLOWUP'
    )),
    entity_ref TEXT,
    domain_revision INTEGER NOT NULL CHECK (domain_revision >= 0),
    plan_id TEXT,
    plan_revision TEXT,
    scheduled_for TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING','SNOOZED','DELIVERED','SUPPRESSED','FAILED')),
    initial_notification_id TEXT,
    group_key TEXT,
    cooldown_until TEXT,
    snoozed_until TEXT,
    delivered_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, suppression_key),
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, initial_notification_id) REFERENCES notifications(account_id, id),
    CHECK ((plan_id IS NULL AND plan_revision IS NULL) OR (plan_id IS NOT NULL AND plan_revision IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_notifications_due
ON notifications(account_id, state, scheduled_for, id);
CREATE INDEX IF NOT EXISTS idx_notifications_group
ON notifications(account_id, group_key, created_at, id);
