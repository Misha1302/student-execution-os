from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    ObligationCategory,
)
from student_execution_os.notifications import (
    FCMChannel,
    FCMConfig,
    NotificationKind,
    NotificationState,
    QuietHours,
    SQLiteNotificationRepository,
    register_device,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class Pass8RecurrenceNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(BASE)
        self.repo = SQLiteCanonicalRepository(":memory:", clock=self.clock)
        self.repo.initialize()
        self.repo.create_account("a")
        self.recurrence = SQLiteRecurrenceRepository(self.repo)
        self.notifications = SQLiteNotificationRepository(self.repo)

    def tearDown(self) -> None:
        self.repo.close()

    def create_template(self, **overrides):
        values = dict(
            account_id="a",
            template_id="series",
            title="Recurring seminar",
            dtstart_local=datetime(2026, 9, 21, 10, 0),
            duration_minutes=60,
            recurrence_rule="FREQ=DAILY;COUNT=4",
            timezone_name="UTC",
            actor=ActorCategory.USER_UI,
        )
        values.update(overrides)
        return self.recurrence.create_template(**values)

    def test_at50_moving_one_occurrence_retains_original_identity(self):
        template = self.create_template()
        original_id = "2026-09-22T10:00:00"
        moved = self.recurrence.set_override(
            account_id="a",
            template_id=template.id,
            original_recurrence_id=original_id,
            action=OccurrenceOverrideAction.MODIFY,
            replacement_start_local=datetime(2026, 9, 22, 14, 0),
            actor=ActorCategory.USER_UI,
        )

        occurrences = self.recurrence.expand(
            account_id="a",
            template_id=template.id,
            horizon_start=datetime(2026, 9, 21, 0, tzinfo=UTC),
            horizon_end=datetime(2026, 9, 25, 0, tzinfo=UTC),
        )
        by_identity = {item.original_recurrence_id: item for item in occurrences}
        self.assertEqual(by_identity[original_id].identity, (template.id, original_id))
        self.assertEqual(by_identity[original_id].starts_at.hour, 14)
        self.assertEqual(by_identity[original_id].override_id, moved.id)
        self.assertEqual(by_identity["2026-09-21T10:00:00"].starts_at.hour, 10)
        self.assertEqual(by_identity["2026-09-23T10:00:00"].starts_at.hour, 10)
        unchanged = self.recurrence.get_template("a", template.id)
        self.assertEqual(unchanged.dtstart_local, datetime(2026, 9, 21, 10, 0))
        self.assertEqual(unchanged.recurrence_rule.canonical(), "FREQ=DAILY;COUNT=4")

    def test_at51_this_and_future_split_preserves_historical_occurrences(self):
        template = self.create_template(recurrence_rule="FREQ=DAILY;UNTIL=2026-09-26T10:00:00")
        historical_horizon_start = datetime(2026, 9, 20, 0, tzinfo=UTC)
        historical_horizon_end = datetime(2026, 9, 23, 0, tzinfo=UTC)
        before = self.recurrence.expand(
            account_id="a", template_id=template.id,
            horizon_start=historical_horizon_start, horizon_end=historical_horizon_end,
        )
        before_ids = [(item.identity, item.starts_at) for item in before]

        old, successor = self.recurrence.split_this_and_future(
            account_id="a",
            template_id=template.id,
            original_recurrence_id="2026-09-23T10:00:00",
            successor_id="series-v2",
            replacement_start_local=datetime(2026, 9, 23, 11, 0),
            actor=ActorCategory.USER_UI,
        )
        after = self.recurrence.expand(
            account_id="a", template_id=old.id,
            horizon_start=historical_horizon_start, horizon_end=historical_horizon_end,
        )
        self.assertEqual([(item.identity, item.starts_at) for item in after], before_ids)
        self.assertEqual(old.series_end_before_local, datetime(2026, 9, 23, 10, 0))
        self.assertEqual(successor.dtstart_local, datetime(2026, 9, 23, 11, 0))
        future = self.recurrence.expand(
            account_id="a", template_id=successor.id,
            horizon_start=datetime(2026, 9, 23, 0, tzinfo=UTC),
            horizon_end=datetime(2026, 9, 25, 0, tzinfo=UTC),
        )
        self.assertEqual([x.starts_at.hour for x in future], [11, 11])
        self.assertTrue(all(x.template_id == "series-v2" for x in future))

    def test_at52_local_civil_time_is_stable_across_dst_transition(self):
        template = self.create_template(
            dtstart_local=datetime(2026, 10, 18, 9, 0),
            recurrence_rule="FREQ=WEEKLY;COUNT=3",
            timezone_name="Europe/Amsterdam",
        )
        occurrences = self.recurrence.expand(
            account_id="a",
            template_id=template.id,
            horizon_start=datetime(2026, 10, 18, 0, tzinfo=UTC),
            horizon_end=datetime(2026, 11, 2, 0, tzinfo=UTC),
        )
        amsterdam = ZoneInfo("Europe/Amsterdam")
        local_starts = [item.starts_at.astimezone(amsterdam) for item in occurrences]
        self.assertEqual([item.hour for item in local_starts], [9, 9, 9])
        self.assertEqual([item.utcoffset() for item in local_starts], [timedelta(hours=2), timedelta(hours=1), timedelta(hours=1)])
        self.assertEqual(
            [item.starts_at.astimezone(UTC).hour for item in occurrences],
            [7, 8, 8],
        )

    def test_recurring_occurrences_enter_same_planning_and_travel_input_path(self):
        template = self.create_template(
            dtstart_local=datetime(2026, 9, 21, 11, 0),
            recurrence_rule="FREQ=DAILY;COUNT=1",
        )
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=6),
        )
        occurrence_id = f"rec:{template.id}:2026-09-21T11:00:00"
        self.assertIn(occurrence_id, {event.obligation.id for event in snapshot.events})

        before_hash = snapshot.input_hash
        self.recurrence.set_override(
            account_id="a", template_id=template.id,
            original_recurrence_id="2026-09-21T11:00:00",
            action=OccurrenceOverrideAction.MODIFY,
            replacement_start_local=datetime(2026, 9, 21, 12, 0),
            actor=ActorCategory.USER_UI,
        )
        changed = build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=6),
        )
        self.assertNotEqual(before_hash, changed.input_hash)
        event = next(item for item in changed.events if item.obligation.id == occurrence_id)
        self.assertEqual(event.interval.starts_at, datetime(2026, 9, 21, 12, 0, tzinfo=UTC))

    def test_at53_stale_notification_is_suppressed_before_channel_delivery(self):
        notification = self.notifications.schedule(
            account_id="a", suppression_key="deadline:t1:entered-warning",
            kind=NotificationKind.DEADLINE_WARNING, scheduled_for=BASE,
            domain_revision=self.repo.get_server_revision("a"), entity_ref="t1",
        )
        self.repo.create_task(
            account_id="a", obligation_id="t1", title="Task",
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=30, remaining_effort_minutes=30,
            splittable=False, actual_cutoff=HardCutoff.absent(), actor=ActorCategory.USER_UI,
        )
        sends: list[str] = []
        result = self.notifications.deliver(
            account_id="a", notification_id=notification.id,
            sender=lambda item, _key: sends.append(item.id) or True,
        )
        self.assertEqual(result.state, NotificationState.SUPPRESSED)
        self.assertEqual(result.last_error, "STALE_DOMAIN_REVISION")
        self.assertEqual(sends, [])

    def test_at54_completion_followup_requires_successful_initial_delivery(self):
        revision = self.repo.get_server_revision("a")
        initial = self.notifications.schedule(
            account_id="a", suppression_key="initial:t1", kind=NotificationKind.DEADLINE_WARNING,
            scheduled_for=BASE, domain_revision=revision, entity_ref="t1",
        )
        failed = self.notifications.deliver(
            account_id="a", notification_id=initial.id, sender=lambda _item, _key: False,
        )
        self.assertEqual(failed.state, NotificationState.FAILED)
        followup = self.notifications.schedule(
            account_id="a", suppression_key="followup:t1", kind=NotificationKind.COMPLETION_FOLLOWUP,
            scheduled_for=BASE, domain_revision=revision, entity_ref="t1",
            initial_notification_id=initial.id,
        )
        sends: list[str] = []
        blocked = self.notifications.deliver(
            account_id="a", notification_id=followup.id,
            sender=lambda item, _key: sends.append(item.id) or True,
        )
        self.assertEqual(blocked.state, NotificationState.SUPPRESSED)
        self.assertEqual(blocked.last_error, "INITIAL_NOTIFICATION_NOT_DELIVERED")
        self.assertEqual(sends, [])

    def test_at55_snooze_changes_workflow_time_only(self):
        task = self.repo.create_task(
            account_id="a", obligation_id="t1", title="Task",
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=30, remaining_effort_minutes=30,
            splittable=False, target_at=BASE + timedelta(hours=4),
            actual_cutoff=HardCutoff.known(BASE + timedelta(hours=6)), actor=ActorCategory.USER_UI,
        )
        revision = self.repo.get_server_revision("a")
        notification = self.notifications.schedule(
            account_id="a", suppression_key="deadline:t1", kind=NotificationKind.DEADLINE_WARNING,
            scheduled_for=BASE + timedelta(minutes=10), domain_revision=revision, entity_ref="t1",
        )
        snoozed = self.notifications.snooze(
            account_id="a", notification_id=notification.id, until=BASE + timedelta(hours=1),
            expected_version=notification.version,
        )
        unchanged = self.repo.get_task("a", task.obligation.id)
        self.assertEqual(snoozed.state, NotificationState.SNOOZED)
        self.assertEqual(snoozed.snoozed_until, BASE + timedelta(hours=1))
        self.assertEqual(unchanged.target_at, task.target_at)
        self.assertEqual(unchanged.actual_cutoff, task.actual_cutoff)
        self.assertEqual(self.repo.get_server_revision("a"), revision)

    def test_at68_same_logical_transition_is_rebound_without_duplicate_notification(self):
        key = self.notifications.transition_suppression_key(
            kind=NotificationKind.RISK_THRESHOLD,
            entity_ref="t1",
            transition_token="SAFE->START_SOON",
        )
        first = self.notifications.schedule(
            account_id="a", suppression_key=key, kind=NotificationKind.RISK_THRESHOLD,
            scheduled_for=BASE + timedelta(minutes=5), domain_revision=self.repo.get_server_revision("a"),
            entity_ref="t1", group_key="risk:t1", cooldown_until=BASE + timedelta(minutes=15),
        )
        self.repo.create_task(
            account_id="a", obligation_id="unrelated", title="Unrelated",
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=5, remaining_effort_minutes=5,
            splittable=False, actual_cutoff=HardCutoff.absent(), actor=ActorCategory.USER_UI,
        )
        new_revision = self.repo.get_server_revision("a")
        recomputed = self.notifications.schedule(
            account_id="a", suppression_key=key, kind=NotificationKind.RISK_THRESHOLD,
            scheduled_for=BASE + timedelta(minutes=6), domain_revision=new_revision,
            entity_ref="t1", group_key="risk:t1", cooldown_until=BASE + timedelta(minutes=15),
        )
        self.assertEqual(recomputed.id, first.id)
        self.assertEqual(recomputed.domain_revision, new_revision)
        self.assertEqual(recomputed.scheduled_for, BASE + timedelta(minutes=6))
        self.assertEqual(len(self.notifications.list("a")), 1)

    def test_identical_notification_reconciliation_does_not_advance_workflow_version(self):
        values = dict(
            account_id="a", suppression_key="same-transition",
            kind=NotificationKind.RISK_THRESHOLD,
            scheduled_for=BASE + timedelta(minutes=5),
            domain_revision=self.repo.get_server_revision("a"), entity_ref="t1",
            group_key="risk:t1", cooldown_until=BASE + timedelta(minutes=15),
        )
        first = self.notifications.schedule(**values)
        second = self.notifications.schedule(**values)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.version, first.version)
        metric = self.repo.connection.execute(
            "SELECT value FROM operational_metrics WHERE account_id='a' AND metric_name='notification_duplicate_suppressed'"
        ).fetchone()
        self.assertEqual(metric[0], 1)

    def test_quiet_hours_defers_using_iana_civil_time(self):
        quiet = QuietHours(
            timezone_name="Europe/Amsterdam",
            starts_local=time(22, 0),
            ends_local=time(7, 0),
        )
        scheduled = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 around DST fall-back.
        notification = self.notifications.schedule(
            account_id="a", suppression_key="quiet", kind=NotificationKind.SOURCE_CHANGE,
            scheduled_for=scheduled, domain_revision=self.repo.get_server_revision("a"),
            quiet_hours=quiet,
        )
        local = notification.scheduled_for.astimezone(ZoneInfo("Europe/Amsterdam"))
        self.assertEqual((local.hour, local.minute), (7, 0))

    def test_fcm_channel_uses_active_device_and_stable_delivery_key(self):
        register_device(self.repo, "a", {"token": "device-token", "device_id": "phone"})
        notification = self.notifications.schedule(
            account_id="a", suppression_key="fcm", kind=NotificationKind.PLAN_CONFLICT,
            scheduled_for=BASE, domain_revision=self.repo.get_server_revision("a"), group_key="plan",
        )

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"name":"projects/demo/messages/1"}'

        with patch("urllib.request.urlopen", return_value=Response()) as send:
            receipt = FCMChannel(
                FCMConfig("https://fcm.example/messages:send", "secret"), canonical=self.repo
            ).send(notification, "delivery-stable")
        self.assertTrue(receipt.delivered)
        request = send.call_args.args[0]
        self.assertEqual(request.headers["X-idempotency-key"], "delivery-stable:phone")
        self.assertIn(b'"deep_link": "#/today"', request.data)


if __name__ == "__main__":
    unittest.main()
