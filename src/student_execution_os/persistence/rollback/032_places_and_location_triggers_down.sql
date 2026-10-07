-- Roll back schema v32: location triggers are dropped; the routing consent column is
-- removed by rebuilding places with its v6 columns (SQLite < 3.35 has no DROP COLUMN).
DROP TABLE IF EXISTS location_triggers;
DROP TABLE IF EXISTS route_refresh_state;
CREATE TABLE places_v31 (
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
INSERT INTO places_v31(id,account_id,alias,display_name,address,latitude,longitude,visibility_policy,version,created_at,updated_at)
    SELECT id,account_id,alias,display_name,address,latitude,longitude,visibility_policy,version,created_at,updated_at FROM places;
PRAGMA foreign_keys=OFF;
DROP TABLE places;
ALTER TABLE places_v31 RENAME TO places;
PRAGMA foreign_keys=ON;
CREATE INDEX IF NOT EXISTS idx_places_account ON places(account_id, id);
DELETE FROM schema_migrations WHERE version=32;
