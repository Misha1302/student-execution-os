-- v27 -> v26 is intentionally fail-closed: groups hold shared data for several accounts
-- and their schedule is materialized in members' accounts; archive/delete groups first.
CREATE TEMP TABLE _v27_rollback_guard(value INTEGER);
CREATE TEMP TRIGGER _v27_rollback_guard_check
BEFORE INSERT ON _v27_rollback_guard
WHEN EXISTS (SELECT 1 FROM groups)
BEGIN
  SELECT RAISE(ABORT, 'v27 rollback requires every group to be deleted first');
END;
INSERT INTO _v27_rollback_guard VALUES (1);
DROP TRIGGER _v27_rollback_guard_check;
DROP TABLE _v27_rollback_guard;

DROP TABLE group_proposals;
DROP TABLE group_schedule_items;
DROP TABLE group_invitations;
DROP TABLE group_members;
DROP TABLE groups;
DELETE FROM schema_migrations WHERE version=27;
