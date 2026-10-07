-- Roll back schema v33: checklists are dropped; their Tasks are unchanged.
DROP TABLE IF EXISTS task_subtasks;
DELETE FROM schema_migrations WHERE version=33;
