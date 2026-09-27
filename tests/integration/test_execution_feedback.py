from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.execution import SQLiteExecutionStore
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.sync.commands import SyncService


NOW = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)


class ExecutionFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "execution.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_task(
                account_id="a",
                obligation_id="task-execution-1",
                title="LLVM pass",
                description=None,
                category=ObligationCategory.WORK,
                importance=Importance.HIGH,
                estimated_total_effort_minutes=120,
                remaining_effort_minutes=120,
                splittable=True,
                min_chunk_minutes=15,
                max_chunk_minutes=90,
                actionable_from=None,
                target_at=None,
                actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.USER_UI,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, now, op_id, kind, entity, payload=None):
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(now)) as repo:
            repo.initialize()
            return SyncService(repo, account_id="a", principal_id="u", now=now).apply({
                "op_id": op_id,
                "type": kind,
                "entity_id": entity,
                "payload": payload or {},
            })

    def active(self, now):
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(now)) as repo:
            repo.initialize()
            return SQLiteExecutionStore(repo).active("a", now)

    def task(self, now):
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(now)) as repo:
            repo.initialize()
            return repo.get_task("a", "task-execution-1")

    def test_pause_time_is_not_actual_work_and_finish_does_not_change_remaining(self):
        start = self.apply(NOW, "op-start-0001", "execution.start", "execution-0001", {"task_id": "task-execution-1"})
        self.assertEqual(start["status"], "APPLIED")
        self.apply(NOW + timedelta(minutes=30), "op-pause-0001", "execution.pause", "execution-0001")
        self.apply(NOW + timedelta(minutes=60), "op-resume-001", "execution.resume", "execution-0001")
        finished = self.apply(
            NOW + timedelta(minutes=105), "op-finish-001", "execution.finish", "execution-0001",
            {"task_id": "task-execution-1", "outcome": "KEEP_REMAINING"},
        )
        self.assertEqual(finished["entity"]["actual_work_seconds"], 75 * 60)
        self.assertEqual(finished["entity"]["state"], "FINISHED")
        self.assertEqual(self.task(NOW + timedelta(minutes=105)).remaining_effort_minutes, 120)

    def test_user_confirmed_remaining_changes_task(self):
        self.apply(NOW, "op-start-0002", "execution.start", "execution-0002", {"task_id": "task-execution-1"})
        self.apply(
            NOW + timedelta(minutes=40), "op-finish-002", "execution.finish", "execution-0002",
            {"task_id": "task-execution-1", "outcome": "UPDATE_REMAINING", "remaining_effort_minutes": 95},
        )
        self.assertEqual(self.task(NOW + timedelta(minutes=40)).remaining_effort_minutes, 95)

    def test_complete_outcome_closes_task_and_session(self):
        self.apply(NOW, "op-start-0003", "execution.start", "execution-0003", {"task_id": "task-execution-1"})
        result = self.apply(
            NOW + timedelta(minutes=20), "op-finish-003", "execution.finish", "execution-0003",
            {"task_id": "task-execution-1", "outcome": "COMPLETE"},
        )
        self.assertEqual(result["entity"]["state"], "FINISHED")
        self.assertEqual(self.task(NOW + timedelta(minutes=20)).obligation.lifecycle_status.value, "COMPLETED")
        self.assertIsNone(self.active(NOW + timedelta(minutes=20)))

    def test_one_non_terminal_session_per_account(self):
        self.apply(NOW, "op-start-0004", "execution.start", "execution-0004", {"task_id": "task-execution-1"})
        result = self.apply(
            NOW + timedelta(minutes=1), "op-start-0005", "execution.start", "execution-0005",
            {"task_id": "task-execution-1"},
        )
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.active(NOW + timedelta(minutes=1))["id"], "execution-0004")

    def test_replay_is_exactly_once(self):
        op = {
            "op_id": "op-start-replay",
            "type": "execution.start",
            "entity_id": "execution-replay",
            "payload": {"task_id": "task-execution-1"},
        }
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            service = SyncService(repo, account_id="a", principal_id="u", now=NOW)
            first = service.apply(op)
            second = service.apply(op)
            self.assertEqual(first["status"], "APPLIED")
            self.assertTrue(second["replayed"])
            count = repo.connection.execute(
                "SELECT count(*) FROM execution_sessions WHERE account_id='a'"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_active_session_survives_repository_restart(self):
        self.apply(NOW, "op-start-0006", "execution.start", "execution-0006", {"task_id": "task-execution-1"})
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW + timedelta(minutes=7))) as repo:
            repo.initialize()
            active = SQLiteExecutionStore(repo).active("a", NOW + timedelta(minutes=7))
            self.assertEqual(active["id"], "execution-0006")
            self.assertGreaterEqual(active["actual_work_seconds"], 7 * 60)


    def test_offline_batch_keeps_user_reported_execution_times(self):
        replayed_at = NOW + timedelta(hours=4)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(replayed_at)) as repo:
            repo.initialize()
            service = SyncService(repo, account_id="a", principal_id="u", now=replayed_at)
            results = service.apply_batch([
                {"op_id": "op-offline-start", "type": "execution.start", "entity_id": "execution-offline",
                 "payload": {"task_id": "task-execution-1", "occurred_at": NOW.isoformat()}},
                {"op_id": "op-offline-pause", "type": "execution.pause", "entity_id": "execution-offline",
                 "payload": {"occurred_at": (NOW + timedelta(minutes=30)).isoformat()}},
                {"op_id": "op-offline-resume", "type": "execution.resume", "entity_id": "execution-offline",
                 "payload": {"occurred_at": (NOW + timedelta(minutes=60)).isoformat()}},
                {"op_id": "op-offline-finish", "type": "execution.finish", "entity_id": "execution-offline",
                 "payload": {"task_id": "task-execution-1", "outcome": "KEEP_REMAINING",
                             "occurred_at": (NOW + timedelta(minutes=105)).isoformat()}},
            ])
            self.assertTrue(all(item["status"] == "APPLIED" for item in results))
            session = SQLiteExecutionStore(repo).payload("a", "execution-offline", replayed_at)
            self.assertEqual(session["actual_work_seconds"], 75 * 60)
            self.assertEqual(session["started_at"], NOW.isoformat())
            self.assertEqual(session["finished_at"], (NOW + timedelta(minutes=105)).isoformat())


if __name__ == "__main__":
    unittest.main()
