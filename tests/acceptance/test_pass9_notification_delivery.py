from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reminders import PushDispatcher, ReminderStore, SendResult

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    name = "fake"
    configured = True

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def send(self, token, message):
        self.calls.append((token, message))
        return self.outcomes.pop(0)


class NotificationDeliveryOutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "delivery.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize(); repo.create_account("a")
            repo.create_task(account_id="a", obligation_id="task-0001", title="Task",
                estimated_total_effort_minutes=30, remaining_effort_minutes=30,
                category=ObligationCategory.GENERAL, importance=Importance.NORMAL, splittable=False,
                actual_cutoff=HardCutoff.known(NOW + timedelta(hours=1)), actor=ActorCategory.USER_UI)
            store = ReminderStore(repo)
            store.register_device("a", "token", "phone")
            self.message_id = store.add_message("a", stage="DEADLINE_2H", task_ids=["task-0001"],
                content={"title":"Due", "body":"Start", "deep_link":"/task/task-0001", "actions":[]},
                dedupe_key="due", now=NOW)

    def tearDown(self): self.tmp.cleanup()

    def row(self):
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            return dict(repo.connection.execute("SELECT * FROM reminder_messages WHERE id=?", (self.message_id,)).fetchone())

    def test_restart_reclaims_expired_lease(self):
        provider = FakeProvider([SendResult(True, provider_id="provider-1")])
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            self.assertEqual(PushDispatcher(self.db, provider)._claim(repo, NOW, "dead-worker"), [self.message_id])
        stats = PushDispatcher(self.db, provider).run_once(NOW + timedelta(seconds=91), "new-worker")
        self.assertEqual(stats["sent"], 1)
        self.assertEqual(self.row()["delivery_state"], "SENT")

    def test_retry_backoff_is_delivery_not_new_reminder(self):
        failing = FakeProvider([SendResult(False, error="temporary", retryable=True)])
        stats = PushDispatcher(self.db, failing).run_once(NOW, "worker")
        self.assertEqual(stats["retry"], 1)
        row = self.row()
        self.assertEqual(row["delivery_state"], "PENDING")
        self.assertEqual(row["attempts"], 1)
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM reminder_messages").fetchone()[0], 1)

    def test_completion_cancels_pending_delivery_before_provider_call(self):
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            task = repo.get_task("a", "task-0001")
            repo.complete_obligation(account_id="a", obligation_id="task-0001",
                                     expected_version=task.obligation.version, actor=ActorCategory.USER_UI)
        provider = FakeProvider([SendResult(True)])
        stats = PushDispatcher(self.db, provider).run_once(NOW, "worker")
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(self.row()["delivery_state"], "CANCELLED")
        self.assertEqual(provider.calls, [])

    def test_active_lease_is_not_double_claimed(self):
        provider = FakeProvider([])
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            dispatcher = PushDispatcher(self.db, provider)
            self.assertEqual(dispatcher._claim(repo, NOW, "one"), [self.message_id])
            self.assertEqual(dispatcher._claim(repo, NOW + timedelta(seconds=20), "two"), [])


if __name__ == "__main__": unittest.main()
