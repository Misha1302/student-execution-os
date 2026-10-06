-- Roll back schema v31. Reminders materialized by a series stay as ordinary one-shot
-- reminders (they are valid v30 rows); the check-in history and series definitions
-- are dropped with their tables.
DROP TABLE IF EXISTS reminder_series_occurrences;
DROP TABLE IF EXISTS reminder_series;
DROP TABLE IF EXISTS checkin_occurrences;
DROP TABLE IF EXISTS checkin_templates;
DROP TABLE IF EXISTS deleted_entities;
DELETE FROM schema_migrations WHERE version=31;
