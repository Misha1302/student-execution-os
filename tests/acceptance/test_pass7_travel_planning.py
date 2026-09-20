from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    LocationEffect,
    LocationEffectKind,
    HardCutoff,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import (
    FeasibilityEngine,
    FeasibilityStatus,
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


class Pass7TravelPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FrozenClock(BASE)
        self.repo = SQLiteCanonicalRepository(":memory:", clock=self.clock)
        self.repo.initialize()
        self.repo.create_account("a")
        self.travel = SQLiteTravelRepository(self.repo)

    def tearDown(self) -> None:
        self.repo.close()

    def place(self, place_id: str):
        return self.travel.create_place(
            account_id="a",
            place_id=place_id,
            display_name=place_id,
            alias=place_id.upper(),
            visibility_policy="PRIVATE_ALIAS",
            actor=ActorCategory.USER_UI,
        )

    def current(self, place_id: str | None):
        return self.travel.set_current_location(
            account_id="a",
            state=(
                LocationContextState.UNKNOWN
                if place_id is None
                else LocationContextState.KNOWN
            ),
            place_id=place_id,
            source="TEST",
            actor=ActorCategory.USER_UI,
            recorded_at=BASE,
            expires_at=BASE + timedelta(hours=12) if place_id else None,
        )

    def estimate(
        self,
        origin: str,
        destination: str,
        safe_minutes: int,
        *,
        estimate_id: str,
        expires_at: datetime | None = None,
    ):
        return self.travel.add_travel_estimate(
            account_id="a",
            estimate_id=estimate_id,
            origin_place_id=origin,
            destination_place_id=destination,
            transport_mode="TRANSIT",
            expected_duration_minutes=max(1, safe_minutes - 5),
            safe_duration_minutes=safe_minutes,
            source=TravelEstimateSource.ROUTING_PROVIDER,
            source_revision="test",
            actor=ActorCategory.SYSTEM,
            calculated_at=BASE - timedelta(minutes=5),
            expires_at=expires_at or BASE + timedelta(hours=12),
        )

    def stay_event(
        self,
        event_id: str,
        destination: str,
        start: datetime,
        end: datetime,
        *,
        arrival: int = 0,
    ):
        return self.repo.create_fixed_event(
            account_id="a",
            obligation_id=event_id,
            title=event_id,
            starts_at=start,
            ends_at=end,
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(
                kind=LocationEffectKind.STAY,
                destination_place_id=destination,
            ),
            arrival_requirement_minutes=arrival,
            actor=ActorCategory.USER_UI,
        )

    def snapshot(self, end: datetime):
        return build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=end,
            plan_output_horizon_end=end,
        )

    def test_at33_hse_travel_latest_safe_departure_1505(self):
        self.place("hse")
        self.place("psychologist")
        self.current("hse")
        self.estimate("hse", "psychologist", 45, estimate_id="route-hse-psy")
        self.stay_event(
            "psychologist-event",
            "psychologist",
            BASE.replace(hour=16),
            BASE.replace(hour=17),
            arrival=10,
        )

        snapshot = self.snapshot(BASE.replace(hour=18))
        self.assertEqual(snapshot.travel_projection.unknown_reasons, ())
        transition = snapshot.travel_projection.transitions[0]
        self.assertEqual(
            transition.latest_safe_departure,
            BASE.replace(hour=15, minute=5),
        )
        self.assertEqual(
            transition.travel_interval.ends_at,
            BASE.replace(hour=15, minute=50),
        )
        self.assertEqual(
            transition.arrival_buffer.starts_at,
            BASE.replace(hour=15, minute=50),
        )
        self.assertEqual(
            transition.arrival_buffer.ends_at,
            BASE.replace(hour=16),
        )

        plan = Planner(clock=self.clock).plan(snapshot)
        travel_block = next(
            block
            for block in plan.blocks
            if block.type is PlanBlockType.TRAVEL_TRANSITION
        )
        buffer = next(
            block
            for block in plan.blocks
            if block.type is PlanBlockType.BUFFER
        )
        self.assertEqual(travel_block.starts_at, BASE.replace(hour=15, minute=5))
        self.assertEqual(buffer.ends_at, BASE.replace(hour=16))
        store = SQLitePlanStore(self.repo)
        store.save(plan)
        loaded = store.get("a", plan.id)
        self.assertIsNotNone(loaded)
        loaded_travel = next(
            block
            for block in loaded.blocks
            if block.type is PlanBlockType.TRAVEL_TRANSITION
        )
        self.assertEqual(loaded_travel.travel_estimate_id, "route-hse-psy")

    def test_at34_unknown_origin_is_unknown_and_never_fabricated(self):
        self.place("hse")
        self.place("psychologist")
        self.current(None)
        self.estimate("hse", "psychologist", 45, estimate_id="route-hse-psy")
        self.stay_event(
            "psychologist-event",
            "psychologist",
            BASE.replace(hour=16),
            BASE.replace(hour=17),
            arrival=10,
        )

        snapshot = self.snapshot(BASE.replace(hour=18))
        result = FeasibilityEngine().evaluate(snapshot)
        self.assertEqual(result.status, FeasibilityStatus.UNKNOWN)
        self.assertTrue(
            result.reasons[0].startswith("UNKNOWN_CURRENT_LOCATION_FOR_EVENT:")
        )
        self.assertEqual(snapshot.travel_projection.transitions, ())

    def test_at35_no_fake_return_uses_actual_next_location(self):
        for place_id in ("hse", "clinic", "library"):
            self.place(place_id)
        self.current("hse")
        self.estimate("hse", "clinic", 45, estimate_id="route-hse-clinic")
        self.estimate("clinic", "library", 20, estimate_id="route-clinic-library")
        self.stay_event(
            "appointment",
            "clinic",
            BASE.replace(hour=16),
            BASE.replace(hour=17),
        )
        self.stay_event(
            "study",
            "library",
            BASE.replace(hour=18),
            BASE.replace(hour=19),
        )

        snapshot = self.snapshot(BASE.replace(hour=20))
        self.assertEqual(len(snapshot.travel_projection.transitions), 2)
        first, second = snapshot.travel_projection.transitions
        self.assertEqual((first.origin_place_id, first.destination_place_id), ("hse", "clinic"))
        self.assertEqual(
            (second.origin_place_id, second.destination_place_id),
            ("clinic", "library"),
        )
        self.assertFalse(
            any(
                transition.origin_place_id == "clinic"
                and transition.destination_place_id == "hse"
                for transition in snapshot.travel_projection.transitions
            )
        )

    def test_at36_canonical_move_event_changes_location_and_survives_replanning(self):
        for place_id in ("home", "other-city"):
            self.place(place_id)
        self.current("home")
        move = self.repo.create_fixed_event(
            account_id="a",
            obligation_id="booked-train",
            title="Booked train",
            starts_at=BASE.replace(hour=12),
            ends_at=BASE.replace(hour=14),
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(
                kind=LocationEffectKind.MOVE,
                origin_place_id="home",
                destination_place_id="other-city",
            ),
            actor=ActorCategory.USER_UI,
        )
        self.stay_event(
            "hotel-checkin",
            "other-city",
            BASE.replace(hour=15),
            BASE.replace(hour=16),
        )

        first = self.snapshot(BASE.replace(hour=18))
        self.assertEqual(first.travel_projection.transitions, ())
        plan = Planner(clock=self.clock).plan(first)
        move_block = next(
            block
            for block in plan.blocks
            if block.source_event_id == "booked-train"
        )
        self.assertEqual(move_block.type, PlanBlockType.EVENT_PROJECTION)

        self.repo.create_fixed_event(
            account_id="a",
            obligation_id="remote-call",
            title="Remote call",
            starts_at=BASE.replace(hour=10),
            ends_at=BASE.replace(hour=11),
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(kind=LocationEffectKind.REMOTE),
            actor=ActorCategory.USER_UI,
        )
        second = self.snapshot(BASE.replace(hour=18))
        persisted = self.repo.get_event("a", "booked-train")
        self.assertEqual(persisted.obligation.id, move.obligation.id)
        self.assertEqual(persisted.location_effect.kind, LocationEffectKind.MOVE)
        self.assertEqual(
            persisted.location_effect.destination_place_id,
            "other-city",
        )
        self.assertNotEqual(first.input_hash, second.input_hash)

    def test_travel_occupancy_can_make_an_otherwise_feasible_day_infeasible(self):
        self.place("hse")
        self.place("psychologist")
        self.current("hse")
        self.estimate("hse", "psychologist", 45, estimate_id="route-hse-psy")
        self.stay_event(
            "psychologist-event",
            "psychologist",
            BASE.replace(hour=16),
            BASE.replace(hour=17),
            arrival=10,
        )
        self.repo.create_task(
            account_id="a",
            obligation_id="long-task",
            title="Long task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=370,
            remaining_effort_minutes=370,
            splittable=False,
            actual_cutoff=HardCutoff.known(BASE.replace(hour=16)),
            actor=ActorCategory.USER_UI,
        )

        snapshot = self.snapshot(BASE.replace(hour=17))
        result = FeasibilityEngine().evaluate(snapshot)
        self.assertEqual(result.status, FeasibilityStatus.INFEASIBLE)

    def test_location_effect_place_is_account_scoped(self):
        self.repo.create_account("b")
        travel_b = SQLiteTravelRepository(self.repo)
        travel_b.create_place(
            account_id="b",
            place_id="secret-b",
            display_name="Secret B",
            alias="B",
            visibility_policy="PRIVATE_ALIAS",
            actor=ActorCategory.USER_UI,
        )
        with self.assertRaises(EntityNotFound):
            self.repo.create_fixed_event(
                account_id="a",
                obligation_id="cross-account-place",
                title="Cross account",
                starts_at=BASE.replace(hour=12),
                ends_at=BASE.replace(hour=13),
                attendance_policy=AttendancePolicy.REQUIRED,
                location_effect=LocationEffect(
                    kind=LocationEffectKind.STAY,
                    destination_place_id="secret-b",
                ),
                actor=ActorCategory.USER_UI,
            )

    def test_at37_expired_route_estimate_forces_unknown_until_fresh_route_exists(self):
        self.place("hse")
        self.place("psychologist")
        self.current("hse")
        self.estimate(
            "hse",
            "psychologist",
            45,
            estimate_id="expired-route",
            expires_at=BASE - timedelta(minutes=1),
        )
        self.stay_event(
            "psychologist-event",
            "psychologist",
            BASE.replace(hour=16),
            BASE.replace(hour=17),
            arrival=10,
        )

        stale_snapshot = self.snapshot(BASE.replace(hour=18))
        stale = FeasibilityEngine().evaluate(stale_snapshot)
        self.assertEqual(stale.status, FeasibilityStatus.UNKNOWN)
        self.assertTrue(
            stale.reasons[0].startswith("STALE_TRAVEL_ESTIMATE:")
        )

        self.estimate(
            "hse",
            "psychologist",
            50,
            estimate_id="fresh-route",
        )
        fresh_snapshot = self.snapshot(BASE.replace(hour=18))
        fresh = FeasibilityEngine().evaluate(fresh_snapshot)
        self.assertEqual(fresh.status, FeasibilityStatus.FEASIBLE)
        self.assertNotEqual(stale_snapshot.input_hash, fresh_snapshot.input_hash)
        self.assertEqual(
            fresh_snapshot.travel_projection.transitions[0].latest_safe_departure,
            BASE.replace(hour=15),
        )


if __name__ == "__main__":
    unittest.main()