from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import (
    DependencyCycleError,
    DuplicateHardCutoffOwner,
    EntityNotFound,
    UnsupportedCapability,
    ValidationError,
    VersionConflict,
)
from student_execution_os.domain.model import (
    ActorCategory,
    CutoffBoundary,
    CutoffState,
    DependencySuccessorKind,
    EventTimeSemantics,
    HardCutoff,
    Importance,
    LifecycleStatus,
    MilestoneOwnerKind,
    MilestoneRole,
    ObligationCategory,
    ProjectStatus,
    TemporalPrecision,
    UserTimeConstraintType,
)
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.clock = FrozenClock(BASE)
        self.repo = SQLiteCanonicalRepository(self.db, clock=self.clock)
        self.repo.initialize()
        self.repo.create_account("account-a")
        self.repo.create_account("account-b")

    def tearDown(self) -> None:
        self.repo.close()
        self.tmp.cleanup()

    def make_task(self, *, account: str = "account-a", task_id: str | None = None):
        return self.repo.create_task(
            account_id=account,
            obligation_id=task_id,
            title="Compiler homework",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.HIGH,
            estimated_total_effort_minutes=120,
            remaining_effort_minutes=90,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=BASE,
            actual_cutoff=HardCutoff.known(BASE + timedelta(days=2), CutoffBoundary.INCLUSIVE),
            target_at=BASE + timedelta(days=1),
            actor=ActorCategory.USER_UI,
        )


class SQLiteMigrationTests(RepositoryFixture):
    def test_initial_migration_is_versioned_and_reentrant(self) -> None:
        self.assertEqual(self.repo.schema_version(), SCHEMA_VERSION)
        self.repo.initialize()
        self.assertEqual(self.repo.schema_version(), SCHEMA_VERSION)
        rows = self.repo.connection.execute("SELECT version FROM schema_migrations").fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2])


class SQLiteCanonicalRepositoryTests(RepositoryFixture):
    def test_task_target_update_preserves_cutoff_and_actionable_from(self) -> None:
        task = self.make_task()
        original_cutoff = task.actual_cutoff
        original_actionable = task.actionable_from
        updated = self.repo.update_task(
            account_id="account-a",
            obligation_id=task.obligation.id,
            expected_version=task.obligation.version,
            target_at=BASE + timedelta(hours=6),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(updated.actual_cutoff, original_cutoff)
        self.assertEqual(updated.actionable_from, original_actionable)
        self.assertEqual(updated.target_at, BASE + timedelta(hours=6))
        self.assertEqual(updated.obligation.version, 2)

    def test_subtype_update_uses_parent_obligation_version(self) -> None:
        task = self.make_task()
        updated = self.repo.update_task(
            account_id="account-a",
            obligation_id=task.obligation.id,
            expected_version=1,
            remaining_effort_minutes=60,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(updated.remaining_effort_minutes, 60)
        with self.assertRaises(VersionConflict):
            self.repo.update_task(
                account_id="account-a",
                obligation_id=task.obligation.id,
                expected_version=1,
                remaining_effort_minutes=30,
                actor=ActorCategory.USER_UI,
            )
        self.assertEqual(self.repo.get_task("account-a", task.obligation.id).remaining_effort_minutes, 60)

    def test_cross_account_reads_and_membership_do_not_leak(self) -> None:
        task = self.make_task(account="account-a")
        project_b = self.repo.create_project(
            account_id="account-b", title="Other account", actor=ActorCategory.USER_UI
        )
        with self.assertRaises(EntityNotFound):
            self.repo.get_task("account-b", task.obligation.id)
        with self.assertRaises(EntityNotFound):
            self.repo.add_project_member(
                account_id="account-b",
                project_id=project_b.id,
                obligation_id=task.obligation.id,
                expected_version=1,
                actor=ActorCategory.USER_UI,
            )

    def test_lifecycle_cancel_and_reopen_preserve_history_via_versions(self) -> None:
        task = self.make_task()
        cancelled = self.repo.cancel_obligation(
            account_id="account-a",
            obligation_id=task.obligation.id,
            expected_version=1,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(cancelled.lifecycle_status, LifecycleStatus.CANCELLED)
        reopened = self.repo.reopen_obligation(
            account_id="account-a",
            obligation_id=task.obligation.id,
            expected_version=2,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(reopened.lifecycle_status, LifecycleStatus.ACTIVE)
        self.assertEqual(reopened.version, 3)
        self.assertEqual([x["action"] for x in self.repo.list_audit("account-a")], [
            "CREATE_TASK", "CANCEL", "REOPEN"
        ])

    def test_project_is_separate_container_with_own_version(self) -> None:
        task = self.make_task()
        project = self.repo.create_project(
            account_id="account-a", title="Conference", actor=ActorCategory.USER_UI
        )
        self.assertFalse(hasattr(project, "kind"))
        project = self.repo.add_project_member(
            account_id="account-a",
            project_id=project.id,
            obligation_id=task.obligation.id,
            expected_version=1,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(project.version, 2)
        cancelled = self.repo.cancel_project(
            account_id="account-a",
            project_id=project.id,
            expected_version=2,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(cancelled.status, ProjectStatus.CANCELLED)
        reopened = self.repo.reopen_project(
            account_id="account-a",
            project_id=project.id,
            expected_version=3,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(reopened.status, ProjectStatus.ACTIVE)

    def test_dependency_cycle_is_rejected(self) -> None:
        a = self.make_task(task_id="task-a")
        b = self.make_task(task_id="task-b")
        c = self.make_task(task_id="task-c")
        self.repo.add_dependency(
            account_id="account-a",
            predecessor_task_id=a.obligation.id,
            successor_kind=DependencySuccessorKind.TASK,
            successor_id=b.obligation.id,
            actor=ActorCategory.USER_UI,
        )
        self.repo.add_dependency(
            account_id="account-a",
            predecessor_task_id=b.obligation.id,
            successor_kind=DependencySuccessorKind.TASK,
            successor_id=c.obligation.id,
            actor=ActorCategory.USER_UI,
        )
        with self.assertRaises(DependencyCycleError):
            self.repo.add_dependency(
                account_id="account-a",
                predecessor_task_id=c.obligation.id,
                successor_kind=DependencySuccessorKind.TASK,
                successor_id=a.obligation.id,
                actor=ActorCategory.USER_UI,
            )

    def test_penalty_milestone_coexists_with_obligation_cutoff_but_duplicate_final_owner_is_rejected(self) -> None:
        task = self.make_task()
        penalty = self.repo.create_milestone(
            account_id="account-a",
            owner_kind=MilestoneOwnerKind.OBLIGATION,
            owner_id=task.obligation.id,
            title="Penalty starts",
            marker_at=BASE + timedelta(days=1),
            role=MilestoneRole.PENALTY_START,
            consequence="-10%",
            hard_for_planning=False,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(penalty.role, MilestoneRole.PENALTY_START)
        self.assertEqual(self.repo.get_task("account-a", task.obligation.id).actual_cutoff.state, CutoffState.KNOWN)
        with self.assertRaises(DuplicateHardCutoffOwner):
            self.repo.create_milestone(
                account_id="account-a",
                owner_kind=MilestoneOwnerKind.OBLIGATION,
                owner_id=task.obligation.id,
                title="Duplicate final cutoff",
                marker_at=BASE + timedelta(days=2),
                role=MilestoneRole.FINAL_CUTOFF,
                hard_for_planning=True,
                actor=ActorCategory.USER_UI,
            )

    def test_project_may_own_its_own_final_gate(self) -> None:
        project = self.repo.create_project(
            account_id="account-a", title="Research", actor=ActorCategory.USER_UI
        )
        milestone = self.repo.create_milestone(
            account_id="account-a",
            owner_kind=MilestoneOwnerKind.PROJECT,
            owner_id=project.id,
            title="Project submission closes",
            marker_at=BASE + timedelta(days=10),
            role=MilestoneRole.FINAL_CUTOFF,
            hard_for_planning=True,
            actor=ActorCategory.USER_UI,
        )
        self.assertTrue(milestone.hard_for_planning)

    def test_flexible_event_is_explicitly_unsupported_not_coerced(self) -> None:
        with self.assertRaises(UnsupportedCapability):
            self.repo.create_event(
                account_id="account-a",
                title="Flexible office hours",
                time_semantics=EventTimeSemantics.FLEXIBLE_WINDOW,
                actor=ActorCategory.USER_UI,
            )
        rows = self.repo.connection.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_fixed_event_uses_half_open_interval(self) -> None:
        first = self.repo.create_fixed_event(
            account_id="account-a",
            title="Lecture",
            starts_at=BASE,
            ends_at=BASE + timedelta(hours=1),
            actor=ActorCategory.USER_UI,
        )
        second = self.repo.create_fixed_event(
            account_id="account-a",
            title="Seminar",
            starts_at=BASE + timedelta(hours=1),
            ends_at=BASE + timedelta(hours=2),
            actor=ActorCategory.USER_UI,
        )
        self.assertFalse(first.interval.overlaps(second.interval))

    def test_time_constraint_has_independent_version_and_stale_update_is_rejected(self) -> None:
        constraint = self.repo.create_time_constraint(
            account_id="account-a",
            type=UserTimeConstraintType.UNAVAILABLE,
            starts_at=BASE,
            ends_at=BASE + timedelta(hours=1),
            reason="lunch",
            actor=ActorCategory.USER_UI,
        )
        updated = self.repo.update_time_constraint(
            account_id="account-a",
            constraint_id=constraint.id,
            expected_version=1,
            starts_at=BASE,
            ends_at=BASE + timedelta(hours=2),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(updated.version, 2)
        with self.assertRaises(VersionConflict):
            self.repo.update_time_constraint(
                account_id="account-a",
                constraint_id=constraint.id,
                expected_version=1,
                starts_at=BASE,
                ends_at=BASE + timedelta(hours=3),
                actor=ActorCategory.USER_UI,
            )

    def test_server_revision_is_monotonic_and_actor_is_audited(self) -> None:
        task = self.make_task()
        self.clock.set(BASE + timedelta(minutes=1))
        self.repo.update_task(
            account_id="account-a",
            obligation_id=task.obligation.id,
            expected_version=1,
            remaining_effort_minutes=30,
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(self.repo.get_server_revision("account-a"), 2)
        audit = self.repo.list_audit("account-a")
        self.assertEqual([row["server_revision"] for row in audit], [1, 2])
        self.assertTrue(all(row["actor_category"] == ActorCategory.USER_UI.value for row in audit))

    def test_known_absent_and_unknown_cutoff_roundtrip_without_conflation(self) -> None:
        absent = self.repo.create_task(
            account_id="account-a",
            title="Someday",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.absent(),
            actor=ActorCategory.USER_UI,
        )
        unknown = self.repo.create_task(
            account_id="account-a",
            title="Deadline stated only as Wednesday",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(TemporalPrecision.DATE_ONLY),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(self.repo.get_task("account-a", absent.obligation.id).actual_cutoff.state, CutoffState.ABSENT)
        loaded = self.repo.get_task("account-a", unknown.obligation.id).actual_cutoff
        self.assertEqual(loaded.state, CutoffState.UNKNOWN)
        self.assertEqual(loaded.precision, TemporalPrecision.DATE_ONLY)
        self.assertIsNone(loaded.at)



    def test_schema_rejects_cross_account_project_membership_even_via_raw_sql(self) -> None:
        task = self.make_task(account="account-a")
        project_b = self.repo.create_project(
            account_id="account-b", title="B project", actor=ActorCategory.USER_UI
        )
        with self.assertRaises(Exception) as raised:
            self.repo.connection.execute(
                "INSERT INTO project_members(account_id,project_id,obligation_id) VALUES (?,?,?)",
                ("account-b", project_b.id, task.obligation.id),
            )
        self.assertIn("FOREIGN KEY", str(raised.exception).upper())

    def test_schema_rejects_obligation_final_cutoff_milestone_even_via_raw_sql(self) -> None:
        task = self.make_task()
        with self.assertRaises(Exception) as raised:
            self.repo.connection.execute(
                "INSERT INTO milestones(id,account_id,owner_kind,owner_id,title,marker_at,role,consequence,hard_for_planning,status,version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "raw-final", "account-a", "OBLIGATION", task.obligation.id, "Bad duplicate",
                    (BASE + timedelta(days=2)).isoformat(), "FINAL_CUTOFF", None, 1, "ACTIVE", 1,
                ),
            )
        self.assertIn("CHECK", str(raised.exception).upper())

    def test_event_update_uses_parent_obligation_version(self) -> None:
        event = self.repo.create_fixed_event(
            account_id="account-a",
            title="Lecture",
            starts_at=BASE,
            ends_at=BASE + timedelta(hours=1),
            actor=ActorCategory.USER_UI,
        )
        updated = self.repo.update_fixed_event(
            account_id="account-a",
            obligation_id=event.obligation.id,
            expected_version=1,
            starts_at=BASE + timedelta(minutes=30),
            ends_at=BASE + timedelta(hours=2),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(updated.obligation.version, 2)
        self.assertEqual(updated.interval.starts_at, BASE + timedelta(minutes=30))
        with self.assertRaises(VersionConflict):
            self.repo.update_fixed_event(
                account_id="account-a",
                obligation_id=event.obligation.id,
                expected_version=1,
                starts_at=BASE,
                ends_at=BASE + timedelta(hours=3),
                actor=ActorCategory.USER_UI,
            )

    def test_milestone_has_own_optimistic_version(self) -> None:
        project = self.repo.create_project(
            account_id="account-a", title="Project", actor=ActorCategory.USER_UI
        )
        milestone = self.repo.create_milestone(
            account_id="account-a",
            owner_kind=MilestoneOwnerKind.PROJECT,
            owner_id=project.id,
            title="Gate",
            marker_at=BASE + timedelta(days=4),
            role=MilestoneRole.INTERMEDIATE,
            actor=ActorCategory.USER_UI,
        )
        updated = self.repo.update_milestone(
            account_id="account-a",
            milestone_id=milestone.id,
            expected_version=1,
            marker_at=BASE + timedelta(days=5),
            actor=ActorCategory.USER_UI,
        )
        self.assertEqual(updated.version, 2)
        with self.assertRaises(VersionConflict):
            self.repo.update_milestone(
                account_id="account-a",
                milestone_id=milestone.id,
                expected_version=1,
                marker_at=BASE + timedelta(days=6),
                actor=ActorCategory.USER_UI,
            )

    def test_pinned_work_must_reference_task_not_event(self) -> None:
        event = self.repo.create_fixed_event(
            account_id="account-a",
            title="Lecture",
            starts_at=BASE,
            ends_at=BASE + timedelta(hours=1),
            actor=ActorCategory.USER_UI,
        )
        with self.assertRaises(ValidationError):
            self.repo.create_time_constraint(
                account_id="account-a",
                type=UserTimeConstraintType.PINNED_WORK,
                starts_at=BASE + timedelta(hours=2),
                ends_at=BASE + timedelta(hours=3),
                obligation_id=event.obligation.id,
                actor=ActorCategory.USER_UI,
            )

    def test_state_survives_repository_reopen(self) -> None:
        task = self.make_task()
        task_id = task.obligation.id
        self.repo.close()
        self.repo = SQLiteCanonicalRepository(self.db, clock=self.clock)
        self.repo.initialize()
        loaded = self.repo.get_task("account-a", task_id)
        self.assertEqual(loaded.obligation.id, task_id)
        self.assertEqual(loaded.actual_cutoff.state, CutoffState.KNOWN)
        self.assertEqual(self.repo.schema_version(), SCHEMA_VERSION)

    def test_non_splittable_chunk_configuration_must_allow_one_block(self) -> None:
        with self.assertRaises(ValidationError):
            self.repo.create_task(
                account_id="account-a",
                title="Impossible local shape",
                category=ObligationCategory.GENERAL,
                importance=Importance.NORMAL,
                estimated_total_effort_minutes=90,
                remaining_effort_minutes=90,
                splittable=False,
                max_chunk_minutes=60,
                actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.USER_UI,
            )


if __name__ == "__main__":
    unittest.main()
