ALTER TABLE events
ADD COLUMN arrival_requirement_minutes INTEGER NOT NULL DEFAULT 0
CHECK (arrival_requirement_minutes >= 0);

CREATE TABLE IF NOT EXISTS places (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    alias TEXT,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    address TEXT,
    latitude REAL,
    longitude REAL,
    visibility_policy TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, id),
    CHECK ((latitude IS NULL AND longitude IS NULL) OR (latitude IS NOT NULL AND longitude IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_places_account ON places(account_id, id);

CREATE TABLE IF NOT EXISTS current_location_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('KNOWN','ASSUMED','UNKNOWN')),
    place_id TEXT,
    recorded_at TEXT NOT NULL,
    expires_at TEXT,
    source TEXT NOT NULL,
    FOREIGN KEY(account_id, place_id) REFERENCES places(account_id, id),
    CHECK (
      (state='UNKNOWN' AND place_id IS NULL)
      OR
      (state IN ('KNOWN','ASSUMED') AND place_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_current_location_account
ON current_location_context(account_id, id);

CREATE TABLE IF NOT EXISTS travel_estimates (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    origin_place_id TEXT NOT NULL,
    destination_place_id TEXT NOT NULL,
    departure_time_or_bucket TEXT,
    transport_mode TEXT NOT NULL,
    expected_duration_minutes INTEGER NOT NULL CHECK (expected_duration_minutes > 0),
    safe_duration_minutes INTEGER NOT NULL CHECK (safe_duration_minutes >= expected_duration_minutes),
    source TEXT NOT NULL CHECK (source IN ('ROUTING_PROVIDER','USER_OVERRIDE','LEARNED','FALLBACK')),
    source_revision TEXT,
    calculated_at TEXT NOT NULL,
    expires_at TEXT,
    UNIQUE(account_id, id),
    FOREIGN KEY(account_id, origin_place_id) REFERENCES places(account_id, id),
    FOREIGN KEY(account_id, destination_place_id) REFERENCES places(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_travel_estimates_route
ON travel_estimates(account_id, origin_place_id, destination_place_id, calculated_at, id);

ALTER TABLE plan_blocks RENAME TO plan_blocks_pre_travel;

CREATE TABLE plan_blocks (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plan_snapshots(id) ON DELETE CASCADE,
    block_type TEXT NOT NULL CHECK (
      block_type IN ('WORK','EVENT_PROJECTION','TRAVEL_TRANSITION','BUFFER')
    ),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    obligation_id TEXT,
    source_constraint_ids_json TEXT NOT NULL,
    source_event_id TEXT,
    travel_estimate_id TEXT,
    explanation TEXT NOT NULL,
    CHECK (starts_at < ends_at)
);

INSERT INTO plan_blocks(
    id,plan_id,block_type,starts_at,ends_at,obligation_id,
    source_constraint_ids_json,source_event_id,travel_estimate_id,explanation
)
SELECT
    id,plan_id,block_type,starts_at,ends_at,obligation_id,
    source_constraint_ids_json,source_event_id,NULL,explanation
FROM plan_blocks_pre_travel;

DROP TABLE plan_blocks_pre_travel;

CREATE INDEX idx_plan_blocks_plan ON plan_blocks(plan_id, starts_at);
