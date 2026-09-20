from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import (
    PlanningPolicy,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class PlanningSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE))
        self.repo.initialize()
        self.repo.create_account("a")
        self.task = self.repo.create_task(
            account_id="a",
            obligation_id="t",
            title="t",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actual_cutoff=HardCutoff.known(BASE + timedelta(hours=4)),
            actor=ActorCategory.SYSTEM,
        )

    def tearDown(self):
        self.repo.close()

    def build(self, policy=None):
        return build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=4),
            plan_output_horizon_end=BASE + timedelta(hours=2),
            policy=policy,
        )

    def test_identical_inputs_have_identical_hash(self):
        a = self.build()
        b = self.build()
        self.assertEqual(a.input_hash, b.input_hash)
        self.assertEqual(a.input_server_revision, b.input_server_revision)

    def test_planning_relevant_mutation_changes_revision_and_hash(self):
        a = self.build()
        self.repo.update_task(
            account_id="a",
            obligation_id="t",
            expected_version=self.task.obligation.version,
            remaining_effort_minutes=30,
            actor=ActorCategory.SYSTEM,
        )
        b = self.build()
        self.assertGreater(b.input_server_revision, a.input_server_revision)
        self.assertNotEqual(a.input_hash, b.input_hash)

    def test_policy_version_changes_hash(self):
        self.assertNotEqual(
            self.build(PlanningPolicy("v1")).input_hash,
            self.build(PlanningPolicy("v2")).input_hash,
        )

    def test_snapshot_retries_when_revision_changes_during_capture(self):
        base = SQLitePlanningStateSource(self.repo)

        class ChangingRevisionSource:
            def __init__(self):
                self.revisions = iter((1, 2, 2, 2))
                self.task_reads = 0

            def get_server_revision(self, account_id):
                return next(self.revisions)

            def list_tasks(self, account_id):
                self.task_reads += 1
                return base.list_tasks(account_id)

            def list_events(self, account_id):
                return base.list_events(account_id)

            def list_dependencies(self, account_id):
                return base.list_dependencies(account_id)

            def list_milestones(self, account_id):
                return base.list_milestones(account_id)

            def list_time_constraints(self, account_id):
                return base.list_time_constraints(account_id)

        source = ChangingRevisionSource()
        snapshot = build_planning_snapshot(
            source,
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=4),
        )
        self.assertEqual(snapshot.input_server_revision, 2)
        self.assertEqual(source.task_reads, 2)

    def test_snapshot_fails_closed_when_revision_never_stabilizes(self):
        base = SQLitePlanningStateSource(self.repo)

        class UnstableRevisionSource:
            def __init__(self):
                self.revision = 0
                self.task_reads = 0

            def get_server_revision(self, account_id):
                self.revision += 1
                return self.revision

            def list_tasks(self, account_id):
                self.task_reads += 1
                return base.list_tasks(account_id)

            def list_events(self, account_id):
                return base.list_events(account_id)

            def list_dependencies(self, account_id):
                return base.list_dependencies(account_id)

            def list_milestones(self, account_id):
                return base.list_milestones(account_id)

            def list_time_constraints(self, account_id):
                return base.list_time_constraints(account_id)

        source = UnstableRevisionSource()
        with self.assertRaisesRegex(RuntimeError, "changed during snapshot capture"):
            build_planning_snapshot(
                source,
                account_id="a",
                analysis_horizon_start=BASE,
                analysis_horizon_end=BASE + timedelta(hours=4),
            )
        self.assertEqual(source.task_reads, 3)


class WitnessSafetyTests(unittest.TestCase):
    def test_invalid_witness_is_detected(self):
        from student_execution_os.planning import WorkPlacement, validate_witness

        repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE))
        repo.initialize()
        repo.create_account("x")
        repo.create_task(
            account_id="x",
            obligation_id="t",
            title="t",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=False,
            actual_cutoff=HardCutoff.known(BASE + timedelta(hours=2)),
            actor=ActorCategory.SYSTEM,
        )
        snap = build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id="x",
            analysis_horizon_start=BASE,
            analysis_horizon_end=BASE + timedelta(hours=2),
        )
        bad = (WorkPlacement(BASE, BASE + timedelta(minutes=30), "t"),)
        self.assertTrue(validate_witness(snap, bad))
        repo.close()


if __name__ == "__main__":
    unittest.main()
