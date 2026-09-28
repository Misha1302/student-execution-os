-- v25 -> v24 is intentionally fail-closed: revoke every capability grant first, so a
-- rollback never silently discards credentials an external agent may still be holding.
CREATE TEMP TABLE _v25_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER _v25_rollback_guard_check
BEFORE INSERT ON _v25_rollback_guard
WHEN EXISTS (SELECT 1 FROM capability_grants WHERE revoked_at IS NULL)
BEGIN
  SELECT RAISE(ABORT, 'v25 rollback requires every capability grant to be revoked first');
END;
INSERT INTO _v25_rollback_guard VALUES (1);
DROP TRIGGER _v25_rollback_guard_check;
DROP TABLE _v25_rollback_guard;

DROP TABLE capability_grants;
DELETE FROM schema_migrations WHERE version=25;
