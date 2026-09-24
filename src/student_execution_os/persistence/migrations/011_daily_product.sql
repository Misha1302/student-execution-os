PRAGMA foreign_keys = OFF;

ALTER TABLE tasks RENAME TO tasks_v10;

CREATE TABLE tasks (
    obligation_id TEXT PRIMARY KEY REFERENCES obligations(id) ON DELETE CASCADE,
    estimated_total_effort_minutes INTEGER CHECK (estimated_total_effort_minutes IS NULL OR estimated_total_effort_minutes > 0),
    remaining_effort_minutes INTEGER CHECK (remaining_effort_minutes IS NULL OR remaining_effort_minutes >= 0),
    splittable INTEGER NOT NULL DEFAULT 0 CHECK (splittable IN (0,1)),
    min_chunk_minutes INTEGER CHECK (min_chunk_minutes IS NULL OR min_chunk_minutes > 0),
    max_chunk_minutes INTEGER CHECK (max_chunk_minutes IS NULL OR max_chunk_minutes > 0),
    actionable_from TEXT,
    cutoff_state TEXT NOT NULL CHECK (cutoff_state IN ('UNKNOWN','ABSENT','KNOWN')),
    actual_cutoff_at TEXT,
    cutoff_boundary TEXT CHECK (cutoff_boundary IS NULL OR cutoff_boundary IN ('INCLUSIVE','EXCLUSIVE')),
    cutoff_precision TEXT CHECK (cutoff_precision IS NULL OR cutoff_precision IN ('EXACT_INSTANT','DATE_ONLY','BOUNDED','UNKNOWN')),
    target_at TEXT,
    estimated_total_effort_low_minutes INTEGER CHECK (estimated_total_effort_low_minutes IS NULL OR estimated_total_effort_low_minutes > 0),
    estimated_total_effort_high_minutes INTEGER CHECK (estimated_total_effort_high_minutes IS NULL OR estimated_total_effort_high_minutes > 0),
    remaining_effort_low_minutes INTEGER CHECK (remaining_effort_low_minutes IS NULL OR remaining_effort_low_minutes >= 0),
    remaining_effort_high_minutes INTEGER CHECK (remaining_effort_high_minutes IS NULL OR remaining_effort_high_minutes >= 0),
    CHECK (max_chunk_minutes IS NULL OR min_chunk_minutes IS NULL OR max_chunk_minutes >= min_chunk_minutes),
    CHECK (
      (cutoff_state='KNOWN' AND actual_cutoff_at IS NOT NULL AND cutoff_boundary IS NOT NULL AND cutoff_precision='EXACT_INSTANT')
      OR
      (cutoff_state IN ('UNKNOWN','ABSENT') AND actual_cutoff_at IS NULL AND cutoff_boundary IS NULL)
    )
);

INSERT INTO tasks SELECT * FROM tasks_v10;
DROP TABLE tasks_v10;

ALTER TABLE notification_delivery_outbox RENAME TO notification_delivery_outbox_v10;
ALTER TABLE notifications RENAME TO notifications_v10;

CREATE TABLE notifications (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    suppression_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
      'LATEST_SAFE_DEPARTURE','LATEST_SAFE_START','RISK_THRESHOLD','DEADLINE_WARNING',
      'PLAN_CONFLICT','SOURCE_CHANGE','COMPLETION_FOLLOWUP','PREPARATION'
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
    UNIQUE(account_id,suppression_key), UNIQUE(account_id,id),
    FOREIGN KEY(account_id,initial_notification_id) REFERENCES notifications(account_id,id),
    CHECK ((plan_id IS NULL AND plan_revision IS NULL) OR (plan_id IS NOT NULL AND plan_revision IS NOT NULL))
);
INSERT INTO notifications SELECT * FROM notifications_v10;

CREATE TABLE notification_delivery_outbox (
    notification_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    delivery_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('READY','LEASED','RETRY_WAIT','SENT','SUPPRESSED','DEAD')),
    lease_owner TEXT, lease_expires_at TEXT, next_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    provider_message_id TEXT, last_error TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(account_id,notification_id), UNIQUE(account_id,delivery_key),
    FOREIGN KEY(account_id,notification_id) REFERENCES notifications(account_id,id) ON DELETE CASCADE,
    CHECK ((state='LEASED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
      OR (state!='LEASED' AND lease_owner IS NULL AND lease_expires_at IS NULL))
);
INSERT INTO notification_delivery_outbox SELECT * FROM notification_delivery_outbox_v10;
DROP TABLE notification_delivery_outbox_v10;
DROP TABLE notifications_v10;
CREATE INDEX idx_notifications_due ON notifications(account_id,state,scheduled_for,id);
CREATE INDEX idx_notifications_group ON notifications(account_id,group_key,created_at,id);
CREATE INDEX idx_notification_delivery_due ON notification_delivery_outbox(account_id,state,next_attempt_at,notification_id);

ALTER TABLE events ADD COLUMN selected_location_option_id TEXT;

CREATE TABLE event_location_options (
    id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    location_effect_kind TEXT NOT NULL CHECK (location_effect_kind IN ('NONE','REMOTE','STAY','MOVE')),
    origin_place_id TEXT,
    destination_place_id TEXT,
    PRIMARY KEY(account_id,event_id,id),
    FOREIGN KEY(account_id,event_id) REFERENCES obligations(account_id,id) ON DELETE CASCADE,
    FOREIGN KEY(account_id,origin_place_id) REFERENCES places(account_id,id),
    FOREIGN KEY(account_id,destination_place_id) REFERENCES places(account_id,id),
    CHECK (
      (location_effect_kind IN ('NONE','REMOTE') AND origin_place_id IS NULL AND destination_place_id IS NULL)
      OR (location_effect_kind='STAY' AND origin_place_id IS NULL AND destination_place_id IS NOT NULL)
      OR (location_effect_kind='MOVE' AND origin_place_id IS NOT NULL AND destination_place_id IS NOT NULL)
    )
);

CREATE TABLE planning_profiles (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    first_day_of_week INTEGER NOT NULL DEFAULT 1 CHECK (first_day_of_week BETWEEN 1 AND 7),
    windows_json TEXT NOT NULL DEFAULT '{"1":[["08:00","22:00"]],"2":[["08:00","22:00"]],"3":[["08:00","22:00"]],"4":[["08:00","22:00"]],"5":[["08:00","22:00"]],"6":[["08:00","22:00"]],"7":[["08:00","22:00"]]}',
    optional_event_policy TEXT NOT NULL DEFAULT 'FAIL_CLOSED' CHECK (optional_event_policy IN ('FAIL_CLOSED','OMIT_OPTIONAL','OMIT_OPTIONAL_AND_PREFERRED')),
    policy_disclosure TEXT NOT NULL DEFAULT 'Default planning window 08:00-22:00 local time',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE notification_preferences (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    quiet_starts_local TEXT NOT NULL DEFAULT '22:00',
    quiet_ends_local TEXT NOT NULL DEFAULT '08:00',
    lead_times_json TEXT NOT NULL DEFAULT '{"DEADLINE_WARNING":1440,"LATEST_SAFE_START":0,"LATEST_SAFE_DEPARTURE":0,"RISK_THRESHOLD":0,"PLAN_CONFLICT":0}',
    enabled_kinds_json TEXT NOT NULL DEFAULT '["DEADLINE_WARNING","LATEST_SAFE_START","LATEST_SAFE_DEPARTURE","RISK_THRESHOLD","PLAN_CONFLICT","COMPLETION_FOLLOWUP"]',
    grouping_minutes INTEGER NOT NULL DEFAULT 15 CHECK (grouping_minutes >= 0),
    cooldown_minutes INTEGER NOT NULL DEFAULT 60 CHECK (cooldown_minutes >= 0),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE mobile_devices (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('ANDROID')),
    token_hash TEXT NOT NULL,
    token TEXT NOT NULL,
    label TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id,token_hash),
    UNIQUE(account_id,id)
);

CREATE TABLE assistant_batches (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    redacted_input TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(account_id,id)
);

CREATE TABLE assistant_apply_records (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,principal_id,idempotency_key)
);

CREATE TABLE attachment_blobs (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 0 AND 10485760),
    content BLOB NOT NULL,
    source_record_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id,id),
    FOREIGN KEY(account_id,source_record_id) REFERENCES source_records(account_id,id) ON DELETE CASCADE
);
CREATE INDEX idx_attachment_quota ON attachment_blobs(account_id,size_bytes);

CREATE TABLE attachment_links (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    attachment_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('OBLIGATION','SOURCE_RECORD')),
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(account_id,attachment_id,owner_kind,owner_id),
    FOREIGN KEY(account_id,attachment_id) REFERENCES attachment_blobs(account_id,id) ON DELETE CASCADE
);

CREATE TABLE saved_task_views (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version=1),
    definition_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id,id),
    UNIQUE(account_id,name)
);

CREATE TABLE operational_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
    correlation_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE INDEX idx_mobile_devices_account ON mobile_devices(account_id,active);
CREATE INDEX idx_assistant_batches_account ON assistant_batches(account_id,created_at);
CREATE INDEX idx_attachment_links_owner ON attachment_links(account_id,owner_kind,owner_id);
CREATE INDEX idx_saved_task_views_account ON saved_task_views(account_id,name);
CREATE INDEX idx_operational_metrics_account ON operational_metrics(account_id,recorded_at);

PRAGMA foreign_keys = ON;
