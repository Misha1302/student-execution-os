from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    DependencySuccessorKind,
    HardCutoff,
    Importance,
    ObligationCategory,
    UserTimeConstraintType,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import FeasibilityEngine, FeasibilityStatus, SQLitePlanningStateSource, build_planning_snapshot

UTC=timezone.utc
BASE=datetime(2026,9,20,9,0,tzinfo=UTC)

class Pass2FeasibilityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.repo=SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE))
        self.repo.initialize(); self.repo.create_account("a")
    def tearDown(self): self.repo.close()
    def task(self, tid, minutes, cutoff, *, splittable=False, minc=None, maxc=None, actionable=None, cutoff_obj=None):
        return self.repo.create_task(account_id="a", obligation_id=tid, title=tid,
            category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
            estimated_total_effort_minutes=max(minutes,1), remaining_effort_minutes=minutes,
            splittable=splittable, min_chunk_minutes=minc, max_chunk_minutes=maxc,
            actionable_from=actionable, target_at=None,
            actual_cutoff=cutoff_obj or HardCutoff.known(cutoff), actor=ActorCategory.SYSTEM)
    def snap(self, end=BASE+timedelta(hours=4), display_end=None):
        return build_planning_snapshot(SQLitePlanningStateSource(self.repo), account_id="a", analysis_horizon_start=BASE,
            analysis_horizon_end=end, plan_output_horizon_end=display_end or end)

    def test_at12_actionable_from(self):
        self.task("t",30,BASE+timedelta(hours=3), actionable=BASE+timedelta(hours=1))
        r=FeasibilityEngine().evaluate(self.snap())
        self.assertEqual(r.status,FeasibilityStatus.FEASIBLE)
        self.assertGreaterEqual(min(p.starts_at for p in r.witness if p.task_id=="t"), BASE+timedelta(hours=1))

    def test_at15_dependency_orders_predecessor_before_successor(self):
        self.task("pre",30,BASE+timedelta(hours=3)); self.task("succ",30,BASE+timedelta(hours=3))
        self.repo.add_dependency(account_id="a",predecessor_task_id="pre",successor_kind=DependencySuccessorKind.TASK,
            successor_id="succ",actor=ActorCategory.SYSTEM,dependency_id="d")
        r=FeasibilityEngine().evaluate(self.snap())
        self.assertEqual(r.status,FeasibilityStatus.FEASIBLE)
        pre=max(p.ends_at for p in r.witness if p.task_id=="pre")
        succ=min(p.starts_at for p in r.witness if p.task_id=="succ")
        self.assertLessEqual(pre,succ)

    def test_at19_and_at79_fragmented_capacity_not_feasible_for_non_splittable(self):
        self.task("t",90,BASE+timedelta(hours=3),splittable=False)
        self.repo.create_time_constraint(account_id="a",type=UserTimeConstraintType.UNAVAILABLE,
            starts_at=BASE+timedelta(hours=1),ends_at=BASE+timedelta(hours=2),actor=ActorCategory.SYSTEM,constraint_id="c")
        r=FeasibilityEngine().evaluate(self.snap(BASE+timedelta(hours=3)))
        self.assertEqual(r.status,FeasibilityStatus.INFEASIBLE)

    def test_at20_constructive_failure_without_exact_proof_is_unknown(self):
        self.task("a",60,BASE+timedelta(hours=2)); self.task("b",60,BASE+timedelta(hours=1))
        r=FeasibilityEngine(exact_search_enabled=False).evaluate(self.snap(BASE+timedelta(hours=2)))
        self.assertEqual(r.status,FeasibilityStatus.UNKNOWN)

    def test_at21_exact_fallback_finds_witness_where_greedy_fails(self):
        self.task("a",60,BASE+timedelta(hours=2)); self.task("b",60,BASE+timedelta(hours=1))
        r=FeasibilityEngine().evaluate(self.snap(BASE+timedelta(hours=2)))
        self.assertEqual(r.status,FeasibilityStatus.FEASIBLE)
        self.assertEqual(len(r.witness),2)

    def test_at27_unknown_cutoff_is_unknown(self):
        self.task("t",30,BASE+timedelta(hours=2),cutoff_obj=HardCutoff.unknown())
        r=FeasibilityEngine().evaluate(self.snap())
        self.assertEqual(r.status,FeasibilityStatus.UNKNOWN)
        self.assertTrue(r.reasons[0].startswith("UNKNOWN_HARD_CUTOFF"))

    def test_at58_overlapping_required_events_are_infeasible(self):
        self.repo.create_fixed_event(account_id="a",obligation_id="e1",title="e1",starts_at=BASE,ends_at=BASE+timedelta(hours=1),
            attendance_policy=AttendancePolicy.REQUIRED,actor=ActorCategory.SYSTEM)
        self.repo.create_fixed_event(account_id="a",obligation_id="e2",title="e2",starts_at=BASE+timedelta(minutes=30),ends_at=BASE+timedelta(hours=1,minutes=30),
            attendance_policy=AttendancePolicy.REQUIRED,actor=ActorCategory.SYSTEM)
        r=FeasibilityEngine().evaluate(self.snap())
        self.assertEqual(r.status,FeasibilityStatus.INFEASIBLE)
        self.assertTrue(r.reasons[0].startswith("REQUIRED_EVENT_CONFLICT"))

    def test_at70_search_budget_exhaustion_is_unknown(self):
        self.task("a",60,BASE+timedelta(hours=2)); self.task("b",60,BASE+timedelta(hours=1))
        r=FeasibilityEngine(node_limit=0).evaluate(self.snap(BASE+timedelta(hours=2)))
        self.assertEqual(r.status,FeasibilityStatus.UNKNOWN)
        self.assertEqual(r.reasons,("EXACT_SEARCH_BUDGET_EXHAUSTED",))

    def test_at78_final_residual_chunk_is_legal(self):
        self.task("t",50,BASE+timedelta(hours=1),splittable=True,minc=30,maxc=30)
        r=FeasibilityEngine().evaluate(self.snap(BASE+timedelta(hours=1)))
        self.assertEqual(r.status,FeasibilityStatus.FEASIBLE)
        self.assertEqual(sorted(p.duration_minutes for p in r.witness),[20,30])

    def test_at86_analysis_horizon_extends_beyond_display_horizon(self):
        cutoff=BASE+timedelta(days=14)
        self.task("t",60,cutoff)
        s=build_planning_snapshot(SQLitePlanningStateSource(self.repo),account_id="a",analysis_horizon_start=BASE,
            analysis_horizon_end=BASE+timedelta(days=7),plan_output_horizon_end=BASE+timedelta(days=7))
        self.assertEqual(s.analysis_horizon_end,cutoff)
        self.assertEqual(s.plan_output_horizon_end,BASE+timedelta(days=7))
        self.assertEqual(FeasibilityEngine().evaluate(s).status,FeasibilityStatus.FEASIBLE)

    def test_cutoff_boundary_is_respected_by_feasibility(self):
        from student_execution_os.domain.model import CutoffBoundary
        self.task("t",60,BASE+timedelta(hours=1),cutoff_obj=HardCutoff.known(BASE+timedelta(hours=1),CutoffBoundary.EXCLUSIVE))
        r=FeasibilityEngine().evaluate(self.snap(BASE+timedelta(hours=1)))
        self.assertEqual(r.status,FeasibilityStatus.INFEASIBLE)

    def test_subminute_supported_model_gap_returns_unknown(self):
        self.task("t",30,BASE+timedelta(hours=2),actionable=BASE+timedelta(seconds=30))
        r=FeasibilityEngine().evaluate(self.snap())
        self.assertEqual(r.status,FeasibilityStatus.UNKNOWN)
        self.assertEqual(r.reasons,("UNSUPPORTED_SUB_MINUTE_TIME",))

    def test_no_cutoff_capacity_failure_is_unknown_not_infeasible(self):
        self.task("t",120,BASE+timedelta(hours=1),cutoff_obj=HardCutoff.absent())
        r=FeasibilityEngine().evaluate(self.snap(BASE+timedelta(hours=1)))
        self.assertEqual(r.status,FeasibilityStatus.UNKNOWN)


if __name__=='__main__': unittest.main()
