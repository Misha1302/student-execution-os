from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.reminders import ReminderPrefs, ReminderState, ReminderStore, Stage, TaskFacts, decide

UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class Pass8RecurrenceNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE))
        self.repo.initialize()
        self.repo.create_account("a")
        self.recurrence = SQLiteRecurrenceRepository(self.repo)

    def tearDown(self) -> None:
        self.repo.close()

    def create_template(self, **overrides):
        values = dict(account_id="a", template_id="series", title="Recurring seminar",
                      dtstart_local=datetime(2026, 9, 21, 10), duration_minutes=60,
                      recurrence_rule="FREQ=DAILY;COUNT=4", timezone_name="UTC",
                      actor=ActorCategory.USER_UI)
        values.update(overrides)
        return self.recurrence.create_template(**values)

    def test_at50_moving_one_occurrence_retains_original_identity(self):
        template = self.create_template()
        original = "2026-09-22T10:00:00"
        self.recurrence.set_override(account_id="a", template_id=template.id,
            original_recurrence_id=original, action=OccurrenceOverrideAction.MODIFY,
            replacement_start_local=datetime(2026, 9, 22, 14), actor=ActorCategory.USER_UI)
        occurrences = self.recurrence.expand(account_id="a", template_id=template.id,
            horizon_start=datetime(2026, 9, 21, tzinfo=UTC), horizon_end=datetime(2026, 9, 25, tzinfo=UTC))
        moved = next(item for item in occurrences if item.original_recurrence_id == original)
        self.assertEqual(moved.identity, (template.id, original))
        self.assertEqual(moved.starts_at.hour, 14)

    def test_at51_this_and_future_split_preserves_history(self):
        template = self.create_template(recurrence_rule="FREQ=DAILY;UNTIL=2026-09-26T10:00:00")
        before = self.recurrence.expand(account_id="a", template_id=template.id,
            horizon_start=datetime(2026, 9, 20, tzinfo=UTC), horizon_end=datetime(2026, 9, 23, tzinfo=UTC))
        old, successor = self.recurrence.split_this_and_future(account_id="a", template_id=template.id,
            original_recurrence_id="2026-09-23T10:00:00", successor_id="series-v2",
            replacement_start_local=datetime(2026, 9, 23, 11), actor=ActorCategory.USER_UI)
        after = self.recurrence.expand(account_id="a", template_id=old.id,
            horizon_start=datetime(2026, 9, 20, tzinfo=UTC), horizon_end=datetime(2026, 9, 23, tzinfo=UTC))
        self.assertEqual([(x.identity, x.starts_at) for x in before], [(x.identity, x.starts_at) for x in after])
        self.assertEqual(successor.dtstart_local.hour, 11)

    def test_at52_local_civil_time_survives_dst(self):
        template = self.create_template(dtstart_local=datetime(2026, 10, 18, 9),
                                        recurrence_rule="FREQ=WEEKLY;COUNT=3", timezone_name="Europe/Amsterdam")
        occurrences = self.recurrence.expand(account_id="a", template_id=template.id,
            horizon_start=datetime(2026, 10, 18, tzinfo=UTC), horizon_end=datetime(2026, 11, 2, tzinfo=UTC))
        local = [x.starts_at.astimezone(ZoneInfo("Europe/Amsterdam")) for x in occurrences]
        self.assertEqual([x.hour for x in local], [9, 9, 9])
        self.assertEqual([x.utcoffset() for x in local], [timedelta(hours=2), timedelta(hours=1), timedelta(hours=1)])

    def test_recurring_occurrence_uses_planning_input(self):
        template = self.create_template(dtstart_local=datetime(2026, 9, 21, 11), recurrence_rule="FREQ=DAILY;COUNT=1")
        snapshot = build_planning_snapshot(SQLitePlanningStateSource(self.repo), account_id="a",
                                           analysis_horizon_start=BASE, analysis_horizon_end=BASE + timedelta(hours=6))
        self.assertIn(f"rec:{template.id}:2026-09-21T11:00:00", {e.obligation.id for e in snapshot.events})

    def facts(self, **changes):
        values = dict(task_id="task-0001", title="Task", status="ACTIVE",
                      cutoff_at=BASE + timedelta(hours=2), target_at=None,
                      latest_safe_start=BASE, risk_state="AT_RISK", remaining_minutes=30,
                      started_at=None, last_progress_at=None, actionable_from=None)
        values.update(changes)
        return TaskFacts(**values)

    def test_at53_closed_task_never_produces_reminder(self):
        decision = decide(self.facts(status="COMPLETED"), None, ReminderPrefs(), BASE)
        self.assertIsNone(decision.stage)
        self.assertEqual(decision.state.closed_reason, "COMPLETED")

    def test_at54_execution_loop_changes_after_start_and_progress(self):
        first = decide(self.facts(), None, ReminderPrefs(), BASE)
        self.assertIn(first.stage, {Stage.DEADLINE_2H, Stage.RISK_UP, Stage.START_NOW})
        later = BASE + timedelta(hours=4)
        started = decide(self.facts(cutoff_at=later + timedelta(hours=12), latest_safe_start=BASE,
                                    started_at=BASE, last_progress_at=BASE, risk_state="SAFE"), first.state,
                         ReminderPrefs(), later)
        self.assertEqual(started.stage, Stage.CHECK_IN)

    def test_at55_snooze_changes_workflow_only(self):
        task = self.repo.create_task(account_id="a", obligation_id="task-0001", title="Task",
            estimated_total_effort_minutes=30, remaining_effort_minutes=30,
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL, splittable=False,
            target_at=BASE + timedelta(hours=4), actual_cutoff=HardCutoff.known(BASE + timedelta(hours=6)),
            actor=ActorCategory.USER_UI)
        revision = self.repo.get_server_revision("a")
        ReminderStore(self.repo).touch("a", task.obligation.id, BASE, snooze_until=BASE + timedelta(hours=1))
        unchanged = self.repo.get_task("a", task.obligation.id)
        self.assertEqual(unchanged.target_at, task.target_at)
        self.assertEqual(self.repo.get_server_revision("a"), revision)

    def test_at68_message_dedupe_and_quiet_hours(self):
        store = ReminderStore(self.repo)
        content = {"title": "T", "body": "B", "deep_link": "/today", "actions": []}
        first = store.add_message("a", stage="GROUP", task_ids=["task-0001"], content=content,
                                  dedupe_key="same", now=BASE)
        second = store.add_message("a", stage="GROUP", task_ids=["task-0001"], content=content,
                                   dedupe_key="same", now=BASE)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        prefs = ReminderPrefs(timezone_name="Europe/Amsterdam", quiet_starts_local="22:00", quiet_ends_local="07:00")
        self.assertEqual(prefs.quiet_until(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)).astimezone(ZoneInfo("Europe/Amsterdam")).hour, 7)


if __name__ == "__main__":
    unittest.main()
