from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import (
    DependencyCycleError,
    DuplicateHardCutoffOwner,
    UnsupportedCapability,
    VersionConflict,
)
from student_execution_os.domain.model import (
    ActorCategory,
    CutoffState,
    DependencySuccessorKind,
    EventTimeSemantics,
    HalfOpenInterval,
    HardCutoff,
    Importance,
    MilestoneOwnerKind,
    MilestoneRole,
    ObligationCategory,
    TemporalPrecision,
)
from student_execution_os.persistence import SQLiteCanonicalRepository

UTC = timezone.utc
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class Pass1AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(NOW))
        self.repo.initialize()
        self.repo.create_account("a")

    def tearDown(self) -> None:
        self.repo.close()

    def task(self, task_id: str | None = None, cutoff: HardCutoff | None = None):
        return self.repo.create_task(
            account_id="a",
            obligation_id=task_id,
            title=task_id or "Task",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=True,
            min_chunk_minutes=30,
            actual_cutoff=cutoff or HardCutoff.known(NOW + timedelta(days=2)),
            target_at=NOW + timedelta(days=1),
            actionable_from=NOW,
            actor=ActorCategory.USER_UI,
        )

    def test_at_11_target_vs_cutoff(self) -> None:
        task = self.task()
        cutoff = task.actual_cutoff
        task = self.repo.update_task(
            account_id="a",
            obligation_id=task.obligation.id,
            expected_version=1,
            target_at=NOW + timedelta(hours=2),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(task.actual_cutoff, cutoff)

    def test_at_16_dependency_cycle(self) -> None:
        a = self.task("a-task")
        b = self.task("b-task")
        self.repo.add_dependency(
            account_id="a",
            predecessor_task_id=a.obligation.id,
            successor_kind=DependencySuccessorKind.TASK,
            successor_id=b.obligation.id,
            actor=ActorCategory.USER_UI,
        )
        with self.assertRaises(DependencyCycleError):
            self.repo.add_dependency(
                account_id="a",
                predecessor_task_id=b.obligation.id,
                successor_kind=DependencySuccessorKind.TASK,
                successor_id=a.obligation.id,
                actor=ActorCategory.USER_UI,
            )

    def test_at_17_penalty_marker_and_final_cutoff_are_distinct(self) -> None:
        task = self.task()
        penalty = self.repo.create_milestone(
            account_id="a",
            owner_kind=MilestoneOwnerKind.OBLIGATION,
            owner_id=task.obligation.id,
            title="Penalty begins",
            marker_at=NOW + timedelta(days=1),
            role=MilestoneRole.PENALTY_START,
            consequence="grade penalty",
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(penalty.marker_at, NOW + timedelta(days=1))
        self.assertEqual(self.repo.get_task("a", task.obligation.id).actual_cutoff.at, NOW + timedelta(days=2))

    def test_at_73_no_duplicate_hard_cutoff_owner(self) -> None:
        task = self.task()
        with self.assertRaises(DuplicateHardCutoffOwner):
            self.repo.create_milestone(
                account_id="a",
                owner_kind=MilestoneOwnerKind.OBLIGATION,
                owner_id=task.obligation.id,
                title="Second final cutoff owner",
                marker_at=NOW + timedelta(days=2),
                role=MilestoneRole.FINAL_CUTOFF,
                hard_for_planning=True,
                actor=ActorCategory.USER_UI,
            )

    def test_at_80_aggregate_concurrency_owner(self) -> None:
        task = self.task()
        self.repo.update_task(
            account_id="a",
            obligation_id=task.obligation.id,
            expected_version=1,
            remaining_effort_minutes=30,
            actor=ActorCategory.USER_UI,
        )
        with self.assertRaises(VersionConflict):
            self.repo.update_task(
                account_id="a",
                obligation_id=task.obligation.id,
                expected_version=1,
                remaining_effort_minutes=15,
                actor=ActorCategory.USER_UI,
            )

    def test_at_81_flexible_event_is_not_coerced(self) -> None:
        with self.assertRaises(UnsupportedCapability):
            self.repo.create_event(
                account_id="a",
                title="Flexible event",
                time_semantics=EventTimeSemantics.FLEXIBLE_WINDOW,
                actor=ActorCategory.USER_UI,
            )

    def test_at_83_half_open_event_adjacency(self) -> None:
        first = HalfOpenInterval(NOW, NOW + timedelta(hours=1))
        second = HalfOpenInterval(NOW + timedelta(hours=1), NOW + timedelta(hours=2))
        self.assertFalse(first.overlaps(second))

    def test_at_75_domain_half_known_no_cutoff_vs_unknown(self) -> None:
        absent = self.task("absent", HardCutoff.absent())
        unknown = self.task("unknown", HardCutoff.unknown(TemporalPrecision.DATE_ONLY))
        self.assertEqual(self.repo.get_task("a", absent.obligation.id).actual_cutoff.state, CutoffState.ABSENT)
        unknown_cutoff = self.repo.get_task("a", unknown.obligation.id).actual_cutoff
        self.assertEqual(unknown_cutoff.state, CutoffState.UNKNOWN)
        self.assertEqual(unknown_cutoff.precision, TemporalPrecision.DATE_ONLY)


if __name__ == "__main__":
    unittest.main()
