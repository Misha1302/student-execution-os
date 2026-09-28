-- v24 -> v23 is intentionally fail-closed: disconnect the academic schedule first so
-- encrypted connection material and refresh state are not silently discarded.
CREATE TEMP TABLE _v24_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER _v24_rollback_guard_check
BEFORE INSERT ON _v24_rollback_guard
WHEN EXISTS (SELECT 1 FROM academic_schedule_connections)
BEGIN
  SELECT RAISE(ABORT, 'v24 rollback requires academic schedule connections to be removed first');
END;
INSERT INTO _v24_rollback_guard VALUES (1);
DROP TRIGGER _v24_rollback_guard_check;
DROP TABLE _v24_rollback_guard;

DROP TABLE academic_schedule_connections;
DELETE FROM schema_migrations WHERE version=24;
