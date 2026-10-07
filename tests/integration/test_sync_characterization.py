"""Characterization of the sync replay boundary (§31/§32 of the remediation spec).

These pin behaviour that a decomposition of the command handlers must not change:
the registered operation set, exactly-once replay across a process restart, savepoint
rollback of a failing handler, version races as CONFLICT, and fail-closed unknown types.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.sync.commands import Commands, SyncService

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

REGISTERED = sorted("""
checkin.create checkin.update checkin.end checkin.delete checkin.split
checkin.occurrence.done checkin.occurrence.skip checkin.occurrence.cancel checkin.occurrence.reopen
checkin.occurrence.progress checkin.occurrence.move
reminder_series.create reminder_series.update reminder_series.end reminder_series.delete reminder_series.split
reminder_series.occurrence.skip reminder_series.occurrence.move
place.create place.update place.delete location.set travel.estimate.set
location_trigger.create location_trigger.update location_trigger.fire location_trigger.done
location_trigger.cancel location_trigger.reopen location_trigger.delete
subtask.create subtask.update subtask.complete subtask.reopen subtask.move subtask.delete
calibration.set constraint.create constraint.delete constraint.update
event.cancel event.create event.delete event.reopen event.update
execution.cancel execution.finish execution.pause execution.resume execution.start
intent.close intent.set preference.create preference.delete
milestone.cancel milestone.complete milestone.create milestone.delete milestone.reopen milestone.update
note.archive note.create note.delete note.link note.transcript.fail note.transcript.set note.unarchive note.update
project.cancel project.complete project.create project.member.add project.member.remove project.reopen
project.task.create project.update
reminder.ack reminder.cancel reminder.create reminder.delete reminder.done reminder.reopen reminder.snooze
reminder.update
routine.cancel routine.create routine.occurrence.edit routine.occurrence.reopen routine.occurrence.skip routine.split
series.create series.extra.create series.holiday series.holiday.restore series.occurrence.cancel
series.occurrence.move series.occurrence.restore series.occurrence.update series.split
task.activate task.archive task.cancel task.complete task.create task.defer task.delete task.progress task.reopen
task.restore task.start task.unarchive task.update
""".split())

TASK = {"title": "Отчёт", "estimated_total_effort_minutes": 60, "actual_cutoff": {"state": "UNKNOWN"},
        "splittable": False}


class SyncCharacterizationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temporary.name) / "sync.sqlite")
        self.repository = self.open()
        self.repository.initialize()
        self.repository.create_account("account")

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def open(self):
        return SQLiteCanonicalRepository(self.path, clock=FrozenClock(NOW))

    def service(self, repository=None):
        return SyncService(repository or self.repository, account_id="account", principal_id="device-a")

    def op(self, op_id, op_type, entity_id, payload=None, service=None):
        return (service or self.service()).apply(
            {"op_id": op_id, "type": op_type, "entity_id": entity_id, "payload": payload or {}})

    def count(self, sql, *args):
        return self.repository.connection.execute(sql, args).fetchone()[0]

    def test_every_operation_type_has_exactly_one_registered_handler(self):
        commands = Commands(self.repository, account_id="account", actor=ActorCategory.USER_UI, now=NOW)
        self.assertEqual(sorted(commands.handlers), REGISTERED)

    def test_lost_response_replays_after_restart_without_a_second_mutation(self):
        first = self.op("create-task-0001", "task.create", "task-0001", TASK)
        self.repository.close()
        self.repository = self.open()  # a new process: only the database survives
        replay = self.op("create-task-0001", "task.create", "task-0001", TASK)
        self.assertTrue(replay["replayed"])
        self.assertEqual({**replay, "replayed": False}, first)
        self.assertEqual(self.count("SELECT count(*) FROM obligations WHERE id='task-0001'"), 1)
        reused = self.op("create-task-0001", "task.create", "task-0001", {**TASK, "title": "Другое"})
        self.assertEqual((reused["status"], reused["code"]), ("REJECTED", "OP_ID_REUSED"))
        self.assertEqual(self.count("SELECT count(*) FROM client_operations WHERE op_id='create-task-0001'"), 1)

    def test_a_failing_handler_leaves_no_partial_write_but_records_the_rejection(self):
        self.op("create-task-0002", "task.create", "task-0002", TASK)
        service = self.service()

        def half_done(entity_id, payload):
            self.repository.connection.execute("UPDATE obligations SET title='partial' WHERE id=?", (entity_id,))
            raise ValueError("failed after a write")

        service.commands.handlers["task.update"] = half_done
        result = self.op("update-task-0002", "task.update", "task-0002", {"title": "x"}, service=service)
        self.assertEqual((result["status"], result["code"]), ("REJECTED", "VALIDATION_ERROR"))
        self.assertEqual(self.count("SELECT count(*) FROM obligations WHERE title='partial'"), 0)
        self.assertEqual(self.count("SELECT count(*) FROM client_operations WHERE op_id='update-task-0002'"), 1)
        self.assertTrue(self.op("update-task-0002", "task.update", "task-0002", {"title": "x"})["replayed"])

    def test_a_version_race_is_a_conflict_and_changes_nothing(self):
        starts = NOW + timedelta(days=1)
        created = self.op("create-constraint-1", "constraint.create", "constraint-0001", {
            "type": "UNAVAILABLE", "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=2)).isoformat()})
        version = created["entity"]["version"]
        self.op("update-constraint-1", "constraint.update", "constraint-0001",
                {"reason": "newer", "expected_version": version})
        stale = self.op("update-constraint-2", "constraint.update", "constraint-0001",
                        {"reason": "stale", "expected_version": version})
        self.assertEqual((stale["status"], stale["code"]), ("CONFLICT", "VERSION_CONFLICT"))
        self.assertEqual(self.count("SELECT reason FROM user_time_constraints WHERE id='constraint-0001'"), "newer")

    def test_unknown_operation_type_fails_closed(self):
        result = self.op("unknown-type-01", "task.teleport", "task-0003", {})
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.count("SELECT count(*) FROM obligations"), 0)

    def test_operations_of_other_accounts_are_invisible(self):
        self.repository.create_account("other")
        self.op("create-task-0004", "task.create", "task-0004", TASK)
        other = SyncService(self.repository, account_id="other", principal_id="device-z")
        result = other.apply({"op_id": "complete-other-1", "type": "task.complete", "entity_id": "task-0004", "payload": {}})
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.count("SELECT lifecycle_status FROM obligations WHERE id='task-0004'"), "ACTIVE")


if __name__ == "__main__":
    unittest.main()
