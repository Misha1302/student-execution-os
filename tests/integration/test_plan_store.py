from datetime import datetime, timedelta, timezone
import unittest
from dataclasses import replace

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import Planner, SQLitePlanStore, SQLitePlanningStateSource, build_planning_snapshot

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class PlanStoreIntegrationTests(unittest.TestCase):
    def test_saving_derived_plan_does_not_advance_canonical_revision(self):
        with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_task(
                account_id="a",
                obligation_id="t",
                title="t",
                category=ObligationCategory.GENERAL,
                importance=Importance.NORMAL,
                estimated_total_effort_minutes=30,
                remaining_effort_minutes=30,
                splittable=True,
                min_chunk_minutes=30,
                max_chunk_minutes=30,
                actual_cutoff=HardCutoff.known(BASE + timedelta(hours=2)),
                actor=ActorCategory.USER_UI,
            )
            snapshot = build_planning_snapshot(
                SQLitePlanningStateSource(repo),
                account_id="a",
                analysis_horizon_start=BASE,
                analysis_horizon_end=BASE + timedelta(hours=2),
            )
            before = repo.get_server_revision("a")
            plan = Planner(clock=FrozenClock(BASE)).plan(snapshot)
            store = SQLitePlanStore(repo)
            store.save(plan)
            self.assertEqual(repo.get_server_revision("a"), before)
            self.assertEqual(store.get("a", plan.id), plan)
            self.assertEqual(store.get_current("a", snapshot.input_hash), plan)
            same_projection_new_time = replace(plan, generated_at=BASE + timedelta(minutes=1))
            store.save(same_projection_new_time)
            with self.assertRaisesRegex(RuntimeError, "non-deterministic projection"):
                store.save(replace(plan, explanations=("tampered",)))


if __name__ == "__main__":
    unittest.main()
