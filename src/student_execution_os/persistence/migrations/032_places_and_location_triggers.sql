-- Schema v32: Places as a product surface and typed location triggers (ADR 0035).
--
-- A Place stays the travel owner's (v6). New here:
--   * routing_allowed — the user's explicit consent that this place's exact position
--     may be sent to the configured routing provider. Off by default; an alias, an
--     address and coordinates are three different things (ADR 0035, "privacy levels").
--   * location_triggers — «когда приду домой, напомни разобрать вещи»: a typed
--     ENTER/EXIT trigger on one place. Detection is the Android device's (native
--     geofencing); the server owns the trigger's lifecycle and de-duplicates firings.
ALTER TABLE places ADD COLUMN routing_allowed INTEGER NOT NULL DEFAULT 0 CHECK (routing_allowed IN (0,1));

CREATE TABLE IF NOT EXISTS location_triggers (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    place_id TEXT NOT NULL,
    transition TEXT NOT NULL CHECK (transition IN ('ENTER','EXIT')),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    radius_meters INTEGER NOT NULL DEFAULT 150 CHECK (radius_meters BETWEEN 50 AND 5000),
    -- One-shot by default («когда приду домой» once); a repeating trigger re-arms
    -- after a cool-down so a GPS jitter at the door is not a second arrival.
    repeat INTEGER NOT NULL DEFAULT 0 CHECK (repeat IN (0,1)),
    status TEXT NOT NULL DEFAULT 'ARMED' CHECK (status IN ('ARMED','FIRED','DONE','CANCELLED')),
    fired_at TEXT,
    fire_count INTEGER NOT NULL DEFAULT 0 CHECK (fire_count >= 0),
    completed_at TEXT,
    actor_category TEXT NOT NULL DEFAULT 'USER_UI',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, place_id) REFERENCES places(account_id, id)
);
CREATE INDEX IF NOT EXISTS idx_location_triggers_account ON location_triggers(account_id, status, place_id);
