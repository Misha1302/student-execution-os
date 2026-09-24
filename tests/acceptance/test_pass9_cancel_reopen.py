from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
    UserTimeConstraintType,
)
from student_execution_os.notifications import (
    DeliveryState,
    NotificationKind,
    NotificationState,
    SQLiteNotificationDeliveryOutbox,
    SQLiteNotificationRepository,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import (
    PlanBlockType,
    Planner,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)
from student_execution_os.travel import (
    LocationContextState,
    SQLiteTravelRepository,
    TravelEstimateSource,
)


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class Pass9CancelReopenTests(unittest.TestCase):
    def setUp(self):
        self.clock = FrozenClock(BASE)
        self.repo = SQLiteCanonicalRepository(":memory:", clock=self.clock)
        self.repo.initialize()
        self.repo.create_account("a")

    def tearDown(self):
        self.repo.close()

    def snap(self):
        return build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=8),
        )

    def test_at64_cancel_task_invalidates_future_work_pin_and_notification_but_reopen_restores_planning(self):
        task = self.repo.create_task(
            account_id="a",
            obligation_id="t1",
            title="Task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.known(BASE + timedelta(hours=4)),
            actor=ActorCategory.USER_UI,
        )
        pin = self.repo.create_time_constraint(
            account_id="a",
            constraint_id="pin-t1",
            type=UserTimeConstraintType.PINNED_WORK,
            starts_at=BASE + timedelta(hours=1),
            ends_at=BASE + timedelta(hours=1, minutes=30),
            obligation_id="t1",
            actor=ActorCategory.USER_UI,
        )
        notifications = SQLiteNotificationRepository(self.repo)
        item = notifications.schedule(
            account_id="a",
            suppression_key="task:t1:warning",
            kind=NotificationKind.DEADLINE_WARNING,
            scheduled_for=BASE + timedelta(minutes=15),
            domain_revision=self.repo.get_server_revision("a"),
            entity_ref="t1",
        )
        outbox = SQLiteNotificationDeliveryOutbox(notifications)
        outbox.ensure(item)

        store = SQLitePlanStore(self.repo)
        before_snapshot = self.snap()
        before = Planner(clock=self.clock).plan(before_snapshot)
        store.save(before)
        self.assertTrue(any(b.obligation_id == "t1" for b in before.blocks))
        self.assertIn(pin.id, {c.id for c in before_snapshot.constraints})

        cancelled = self.repo.cancel_obligation(
            account_id="a",
            obligation_id="t1",
            expected_version=task.obligation.version,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(cancelled.lifecycle_status, LifecycleStatus.CANCELLED)

        cancelled_snapshot = self.snap()
        cancelled_plan = Planner(clock=self.clock).plan(cancelled_snapshot, previous_plan=before)
        store.save(cancelled_plan)
        self.assertNotIn("t1", {t.obligation.id for t in cancelled_snapshot.tasks})
        self.assertNotIn(pin.id, {c.id for c in cancelled_snapshot.constraints})
        self.assertFalse(any(b.obligation_id == "t1" for b in cancelled_plan.blocks))
        self.assertEqual(notifications.get("a", item.id).state, NotificationState.SUPPRESSED)
        self.assertEqual(notifications.get("a", item.id).last_error, "OBLIGATION_CANCELLED")
        self.assertEqual(outbox.get("a", item.id).state, DeliveryState.SUPPRESSED)
        self.assertIsNotNone(store.get("a", before.id))

        reopened = self.repo.reopen_obligation(
            account_id="a",
            obligation_id="t1",
            expected_version=cancelled.version,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(reopened.lifecycle_status, LifecycleStatus.ACTIVE)
        reopened_snapshot = self.snap()
        reopened_plan = Planner(clock=self.clock).plan(reopened_snapshot, previous_plan=cancelled_plan)
        self.assertIn("t1", {t.obligation.id for t in reopened_snapshot.tasks})
        self.assertIn(pin.id, {c.id for c in reopened_snapshot.constraints})
        work = next(
            b for b in reopened_plan.blocks
            if b.type is PlanBlockType.WORK and b.obligation_id == "t1"
        )
        self.assertEqual(work.source_constraint_ids, (pin.id,))

        rebound = notifications.schedule(
            account_id="a",
            suppression_key="task:t1:warning",
            kind=NotificationKind.DEADLINE_WARNING,
            scheduled_for=BASE + timedelta(minutes=20),
            domain_revision=self.repo.get_server_revision("a"),
            entity_ref="t1",
        )
        self.assertEqual(rebound.id, item.id)
        self.assertEqual(rebound.state, NotificationState.PENDING)
        self.assertEqual(outbox.get("a", item.id).state, DeliveryState.READY)

    def test_at64_cancel_event_removes_future_travel_but_preserves_old_plan(self):
        travel = SQLiteTravelRepository(self.repo)
        for place_id in ("home", "campus"):
            travel.create_place(
                account_id="a",
                place_id=place_id,
                display_name=place_id,
                alias=place_id.upper(),
                visibility_policy="PRIVATE_ALIAS",
                actor=ActorCategory.SYSTEM,
            )
        travel.set_current_location(
            account_id="a",
            state=LocationContextState.KNOWN,
            place_id="home",
            source="TEST",
            actor=ActorCategory.SYSTEM,
            recorded_at=BASE,
            expires_at=BASE + timedelta(hours=8),
        )
        travel.add_travel_estimate(
            account_id="a",
            estimate_id="route",
            origin_place_id="home",
            destination_place_id="campus",
            transport_mode="TRANSIT",
            expected_duration_minutes=30,
            safe_duration_minutes=40,
            source=TravelEstimateSource.ROUTING_PROVIDER,
            source_revision="r1",
            actor=ActorCategory.SYSTEM,
            calculated_at=BASE,
            expires_at=BASE + timedelta(hours=8),
        )
        event = self.repo.create_fixed_event(
            account_id="a",
            obligation_id="e1",
            title="Lecture",
            starts_at=BASE + timedelta(hours=3),
            ends_at=BASE + timedelta(hours=4),
            location_effect=LocationEffect(
                kind=LocationEffectKind.STAY,
                destination_place_id="campus",
            ),
            arrival_requirement_minutes=10,
            actor=ActorCategory.USER_UI,
        )
        notifications = SQLiteNotificationRepository(self.repo)
        note = notifications.schedule(
            account_id="a",
            suppression_key="event:e1:leave",
            kind=NotificationKind.LATEST_SAFE_DEPARTURE,
            scheduled_for=BASE + timedelta(hours=2),
            domain_revision=self.repo.get_server_revision("a"),
            entity_ref="e1",
        )

        store = SQLitePlanStore(self.repo)
        before_snapshot = self.snap()
        before = Planner(clock=self.clock).plan(before_snapshot)
        store.save(before)
        self.assertTrue(any(
            b.type is PlanBlockType.TRAVEL_TRANSITION and b.source_event_id == "e1"
            for b in before.blocks
        ))

        cancelled = self.repo.cancel_obligation(
            account_id="a",
            obligation_id="e1",
            expected_version=event.obligation.version,
            actor=ActorCategory.USER_UI,
        )
        after_snapshot = self.snap()
        after = Planner(clock=self.clock).plan(after_snapshot, previous_plan=before)
        store.save(after)
        self.assertFalse(any(b.source_event_id == "e1" for b in after.blocks))
        self.assertEqual(after_snapshot.travel_projection.transitions, ())
        self.assertEqual(notifications.get("a", note.id).state, NotificationState.SUPPRESSED)
        self.assertIsNotNone(store.get("a", before.id))

        self.repo.reopen_obligation(
            account_id="a",
            obligation_id="e1",
            expected_version=cancelled.version,
            actor=ActorCategory.USER_UI,
        )
        reopened_snapshot = self.snap()
        self.assertTrue(any(e.obligation.id == "e1" for e in reopened_snapshot.events))
        self.assertTrue(any(
            transition.target_event_id == "e1"
            for transition in reopened_snapshot.travel_projection.transitions
        ))


if __name__ == "__main__":
    unittest.main()
