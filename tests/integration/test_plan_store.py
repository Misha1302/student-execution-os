from datetime import datetime, timedelta, timezone
import unittest
from dataclasses import replace

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning.model import FeasibilityStatus
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
            # A -> B -> A: the rebuilt A carries a replan note but is the same plan.
            other = replace(plan, id="other-plan", input_hash="other-input", plan_revision="other-revision", blocks=())
            store.save(other)
            store.save(replace(plan, explanations=plan.explanations + ("REPLAN_INPUT_CHANGED",)))
            self.assertEqual(store.get_current("a", snapshot.input_hash), plan)
            # Same input, but the search hit its wall-clock budget on a loaded server:
            # never a 500; the complete result wins in either order.
            cut = replace(plan, feasibility_status=FeasibilityStatus.UNKNOWN, blocks=(),
                          explanations=("EXACT_SEARCH_BUDGET_EXHAUSTED",))
            store.save(cut)
            self.assertEqual(store.get("a", plan.id), plan)
            self.assertEqual(store.get_current("a", snapshot.input_hash), plan)
            other_input = replace(plan, id="plan-cut-first", input_hash="cut-first", plan_revision="cut-first",
                                  blocks=())
            store.save(replace(other_input, feasibility_status=FeasibilityStatus.UNKNOWN, blocks=(),
                               explanations=("EXACT_SEARCH_BUDGET_EXHAUSTED",)))
            store.save(other_input)
            self.assertEqual(store.get("a", "plan-cut-first"), other_input)
            self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            # Blocks with different UTC offsets (imported classes carry +03:00) sort by
            # instant, not by string: the same plan read back is the same projection.
            from student_execution_os.planning.model import PlanBlock, PlanBlockType
            msk = timezone(timedelta(hours=3))
            mixed = replace(plan, id="plan-mixed-offsets", input_hash="mixed", plan_revision="mixed", blocks=(
                PlanBlock(id="blk-early-msk", type=PlanBlockType.WORK, obligation_id="t",
                          starts_at=datetime(2026, 9, 1, 10, 0, tzinfo=msk),  # 07:00Z, sorts after "08:.." as text
                          ends_at=datetime(2026, 9, 1, 11, 0, tzinfo=msk), explanation="x"),
                PlanBlock(id="blk-later-utc", type=PlanBlockType.WORK, obligation_id="t",
                          starts_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
                          ends_at=datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc), explanation="y"),
            ))
            store.save(mixed)
            store.save(replace(mixed, explanations=mixed.explanations + ("REPLAN_INPUT_CHANGED",)))
            self.assertEqual([b.id for b in store.get("a", mixed.id).blocks], ["blk-early-msk", "blk-later-utc"])
            # Two complete but different projections are still a real bug.
            with self.assertRaisesRegex(RuntimeError, "non-deterministic projection"):
                store.save(replace(plan, explanations=("tampered",)))


if __name__ == "__main__":
    unittest.main()
