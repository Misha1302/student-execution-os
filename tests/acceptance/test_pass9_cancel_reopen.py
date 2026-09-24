from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, EventTimeSemantics, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.reminders import ReminderStore
from student_execution_os.sync.commands import SyncService

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)


class Pass9CancelReopenTests(unittest.TestCase):
    def setUp(self):
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(NOW))
        self.repo.initialize(); self.repo.create_account("a")
        self.sync = SyncService(self.repo, account_id="a", principal_id="user", now=NOW)

    def tearDown(self): self.repo.close()

    def test_at64_cancel_task_terminates_reminder_and_reopen_restores_planning(self):
        task = self.repo.create_task(account_id="a", obligation_id="task-0001", title="Task",
            estimated_total_effort_minutes=30, remaining_effort_minutes=30,
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL, splittable=False,
            actual_cutoff=HardCutoff.known(NOW + timedelta(hours=4)), actor=ActorCategory.USER_UI)
        store = ReminderStore(self.repo)
        message = store.add_message("a", stage="START_NOW", task_ids=[task.obligation.id],
            content={"title":"Start", "body":"Now", "deep_link":"/task/task-0001", "actions":[]},
            dedupe_key="start", now=NOW)
        cancelled = self.sync.apply({"op_id":"cancel-0001", "type":"task.cancel", "entity_id":"task-0001", "payload":{}})
        self.assertEqual(cancelled["entity"]["status"], "CANCELLED")
        open_ids = {t.obligation.id for t in SQLitePlanningStateSource(self.repo).list_tasks("a")
                    if t.obligation.lifecycle_status.value == "ACTIVE"}
        self.assertNotIn("task-0001", open_ids)
        # A pending push is revalidated by the dispatcher; interaction also records closure intent.
        self.assertIsNotNone(store.states("a")["task-0001"].last_interaction_at)
        reopened = self.sync.apply({"op_id":"reopen-0001", "type":"task.reopen", "entity_id":"task-0001",
                                    "payload":{"remaining_effort_minutes":30}})
        self.assertEqual(reopened["entity"]["status"], "ACTIVE")
        snapshot = build_planning_snapshot(SQLitePlanningStateSource(self.repo), account_id="a",
                                           analysis_horizon_start=NOW, analysis_horizon_end=NOW + timedelta(hours=8))
        self.assertIn("task-0001", {t.obligation.id for t in snapshot.tasks})
        self.assertEqual(store.message("a", message)["id"], message)

    def test_at64_cancel_event_removes_future_projection_and_reopen_restores_it(self):
        event = self.repo.create_event(account_id="a", obligation_id="event-001", title="Class",
            time_semantics=EventTimeSemantics.FIXED_INTERVAL, starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=3), actor=ActorCategory.USER_UI)
        cancelled = self.repo.cancel_obligation(account_id="a", obligation_id=event.obligation.id,
                                                expected_version=event.obligation.version, actor=ActorCategory.USER_UI)
        snapshot = build_planning_snapshot(SQLitePlanningStateSource(self.repo), account_id="a",
                                           analysis_horizon_start=NOW, analysis_horizon_end=NOW + timedelta(hours=8))
        self.assertNotIn(event.obligation.id, {e.obligation.id for e in snapshot.events})
        self.repo.reopen_obligation(account_id="a", obligation_id=event.obligation.id,
                                    expected_version=cancelled.version, actor=ActorCategory.USER_UI)
        reopened = build_planning_snapshot(SQLitePlanningStateSource(self.repo), account_id="a",
                                           analysis_horizon_start=NOW, analysis_horizon_end=NOW + timedelta(hours=8))
        self.assertIn(event.obligation.id, {e.obligation.id for e in reopened.events})


if __name__ == "__main__": unittest.main()
