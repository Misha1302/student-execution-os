from datetime import datetime, timedelta, timezone
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    ObligationCategory,
    UserTimeConstraintType,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import (
    PlanBlockType,
    Planner,
    PlanningPolicy,
    PlanningService,
    RiskEngine,
    RiskState,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
    pin_work_block,
)

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class Pass3AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE))
        self.repo.initialize()
        self.repo.create_account("a")

    def tearDown(self):
        self.repo.close()

    def task(
        self,
        task_id,
        minutes,
        cutoff,
        *,
        low=None,
        high=None,
        actionable=None,
        target=None,
        importance=Importance.NORMAL,
        splittable=True,
        min_chunk=30,
        max_chunk=60,
    ):
        return self.repo.create_task(
            account_id="a",
            obligation_id=task_id,
            title=task_id,
            category=ObligationCategory.GENERAL,
            importance=importance,
            estimated_total_effort_minutes=max(minutes, 1),
            remaining_effort_minutes=minutes,
            remaining_effort_low_minutes=low,
            remaining_effort_high_minutes=high,
            splittable=splittable,
            min_chunk_minutes=min_chunk,
            max_chunk_minutes=max_chunk,
            actionable_from=actionable,
            target_at=target,
            actual_cutoff=cutoff,
            actor=ActorCategory.USER_UI,
        )

    def snap(self, end=BASE + timedelta(hours=6), *, display_end=None, policy=None):
        return build_planning_snapshot(
            SQLitePlanningStateSource(self.repo),
            account_id="a",
            analysis_horizon_start=BASE,
            analysis_horizon_end=end,
            plan_output_horizon_end=display_end or end,
            policy=policy,
        )

    def test_at13_work_block_end_does_not_complete_task(self):
        self.task("t", 30, HardCutoff.known(BASE + timedelta(hours=3)))
        plan = Planner(clock=FrozenClock(BASE)).plan(self.snap())
        self.assertTrue(any(b.type is PlanBlockType.WORK for b in plan.blocks))
        self.assertEqual(self.repo.get_task("a", "t").obligation.lifecycle_status.value, "ACTIVE")

    def test_at14_partial_progress_recomputes_plan_and_input_identity(self):
        task = self.task("t", 60, HardCutoff.known(BASE + timedelta(hours=4)))
        first_snapshot = self.snap()
        first = PlanningService().build(first_snapshot, now=BASE)
        self.repo.update_task(
            account_id="a",
            obligation_id="t",
            expected_version=task.obligation.version,
            remaining_effort_minutes=30,
            actor=ActorCategory.USER_UI,
        )
        second_snapshot = self.snap()
        second = PlanningService().build(second_snapshot, now=BASE, previous_plan=first.plan)
        self.assertNotEqual(first_snapshot.input_hash, second_snapshot.input_hash)
        self.assertFalse(first.plan.is_current_for(second_snapshot))
        self.assertTrue(any(x == "REPLAN_INPUT_CHANGED" for x in second.plan.explanations))
        self.assertEqual(sum(b.duration_minutes for b in second.plan.blocks if b.type is PlanBlockType.WORK), 30)

    def test_at18_project_never_materializes_as_plan_block(self):
        self.task("child", 30, HardCutoff.known(BASE + timedelta(hours=3)))
        project = self.repo.create_project(account_id="a", title="Project", actor=ActorCategory.USER_UI, project_id="p")
        self.repo.add_project_member(
            account_id="a",
            project_id=project.id,
            obligation_id="child",
            expected_version=project.version,
            actor=ActorCategory.USER_UI,
        )
        plan = Planner(clock=FrozenClock(BASE)).plan(self.snap())
        self.assertNotIn("p", {b.obligation_id for b in plan.blocks})
        self.assertIn("child", {b.obligation_id for b in plan.blocks})

    def test_at22_optimistic_infeasible_is_impossible(self):
        self.task("t", 120, HardCutoff.known(BASE + timedelta(hours=1)), low=90, high=150)
        risk = RiskEngine().evaluate(self.snap(BASE + timedelta(hours=1)), BASE)["t"]
        self.assertEqual(risk.state, RiskState.IMPOSSIBLE)

    def test_at23_expected_infeasible_is_critical(self):
        self.task("t", 90, HardCutoff.known(BASE + timedelta(hours=1)), low=60, high=120)
        risk = RiskEngine().evaluate(self.snap(BASE + timedelta(hours=1)), BASE)["t"]
        self.assertEqual(risk.state, RiskState.CRITICAL)

    def test_at24_safe_infeasible_is_at_risk(self):
        self.task("t", 60, HardCutoff.known(BASE + timedelta(hours=1)), low=30, high=90)
        risk = RiskEngine().evaluate(self.snap(BASE + timedelta(hours=1)), BASE)["t"]
        self.assertEqual(risk.state, RiskState.AT_RISK)

    def test_at25_constraint_aware_latest_safe_start_yields_start_soon(self):
        self.task("t", 60, HardCutoff.known(BASE + timedelta(hours=4)), low=60, high=60)
        self.repo.create_time_constraint(
            account_id="a",
            type=UserTimeConstraintType.UNAVAILABLE,
            starts_at=BASE + timedelta(hours=2),
            ends_at=BASE + timedelta(hours=4),
            actor=ActorCategory.USER_UI,
        )
        risk = RiskEngine().evaluate(
            self.snap(BASE + timedelta(hours=4), policy=PlanningPolicy(start_soon_lead_minutes=120)),
            BASE,
        )["t"]
        self.assertEqual(risk.state, RiskState.START_SOON)
        self.assertEqual(risk.latest_safe_start, BASE + timedelta(hours=1))

    def test_at26_safe_with_margin(self):
        self.task("t", 60, HardCutoff.known(BASE + timedelta(hours=6)), low=60, high=60)
        risk = RiskEngine().evaluate(
            self.snap(BASE + timedelta(hours=6), policy=PlanningPolicy(start_soon_lead_minutes=60)),
            BASE,
        )["t"]
        self.assertEqual(risk.state, RiskState.SAFE)
        self.assertGreater(risk.latest_safe_start, BASE + timedelta(hours=1))

    def test_at28_plan_snapshot_invalidation_and_history(self):
        task = self.task("t", 60, HardCutoff.known(BASE + timedelta(hours=4)))
        first_snapshot = self.snap()
        first_plan = Planner(clock=FrozenClock(BASE)).plan(first_snapshot)
        store = SQLitePlanStore(self.repo)
        store.save(first_plan)
        self.assertIsNotNone(store.get_current("a", first_snapshot.input_hash))
        self.repo.update_task(
            account_id="a",
            obligation_id="t",
            expected_version=task.obligation.version,
            remaining_effort_minutes=30,
            actor=ActorCategory.USER_UI,
        )
        second_snapshot = self.snap()
        self.assertIsNone(store.get_current("a", second_snapshot.input_hash))
        second_plan = Planner(clock=FrozenClock(BASE)).plan(second_snapshot, previous_plan=first_plan)
        store.save(second_plan)
        self.assertEqual(len(store.history_ids("a")), 2)
        self.assertIsNotNone(store.get("a", first_plan.id))

    def test_at29_event_projection_change_invalidates_old_plan(self):
        self.task("t", 30, HardCutoff.known(BASE + timedelta(hours=5)))
        event = self.repo.create_fixed_event(
            account_id="a",
            obligation_id="e",
            title="e",
            starts_at=BASE + timedelta(hours=1),
            ends_at=BASE + timedelta(hours=2),
            actor=ActorCategory.USER_UI,
        )
        s1 = self.snap()
        p1 = Planner(clock=FrozenClock(BASE)).plan(s1)
        old_event_block = next(b for b in p1.blocks if b.source_event_id == "e")
        self.repo.update_fixed_event(
            account_id="a",
            obligation_id="e",
            expected_version=event.obligation.version,
            starts_at=BASE + timedelta(hours=2),
            ends_at=BASE + timedelta(hours=3),
            actor=ActorCategory.USER_UI,
        )
        s2 = self.snap()
        p2 = Planner(clock=FrozenClock(BASE)).plan(s2, previous_plan=p1)
        self.assertFalse(p1.is_current_for(s2))
        new_event_block = next(b for b in p2.blocks if b.source_event_id == "e")
        self.assertNotEqual(old_event_block.id, new_event_block.id)

    def test_at30_pin_creates_canonical_time_constraint_not_mutable_plan_state(self):
        self.task("t", 30, HardCutoff.known(BASE + timedelta(hours=3)))
        plan = Planner(clock=FrozenClock(BASE)).plan(self.snap())
        block = next(b for b in plan.blocks if b.type is PlanBlockType.WORK)
        pin = pin_work_block(self.repo, account_id="a", plan=plan, block_id=block.id, actor=ActorCategory.USER_UI)
        self.assertEqual(pin.type, UserTimeConstraintType.PINNED_WORK)
        self.assertEqual(pin.obligation_id, "t")
        self.assertEqual(block.source_constraint_ids, ())

    def test_at31_completion_removes_future_work_but_history_survives(self):
        task = self.task("t", 30, HardCutoff.known(BASE + timedelta(hours=3)))
        store = SQLitePlanStore(self.repo)
        s1 = self.snap()
        p1 = Planner(clock=FrozenClock(BASE)).plan(s1)
        store.save(p1)
        self.repo.complete_obligation(
            account_id="a",
            obligation_id="t",
            expected_version=task.obligation.version,
            actor=ActorCategory.USER_UI,
        )
        s2 = self.snap()
        p2 = Planner(clock=FrozenClock(BASE)).plan(s2, previous_plan=p1)
        store.save(p2)
        self.assertFalse(any(b.obligation_id == "t" and b.type is PlanBlockType.WORK for b in p2.blocks))
        self.assertIsNotNone(store.get("a", p1.id))
        self.assertEqual(len(store.history_ids("a")), 2)

    def test_at32_identical_input_has_equivalent_plan_and_order(self):
        self.task("b", 30, HardCutoff.known(BASE + timedelta(hours=4)), target=BASE + timedelta(hours=3))
        self.task("a", 30, HardCutoff.known(BASE + timedelta(hours=4)), target=BASE + timedelta(hours=2))
        snapshot = self.snap()
        p1 = Planner(clock=FrozenClock(BASE)).plan(snapshot)
        p2 = Planner(clock=FrozenClock(BASE + timedelta(minutes=5))).plan(snapshot)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(p1.plan_revision, p2.plan_revision)
        self.assertEqual(p1.blocks, p2.blocks)

    def test_at75_absent_vs_unknown_cutoff_risk(self):
        self.task("absent", 30, HardCutoff.absent())
        self.task("unknown", 30, HardCutoff.unknown())
        risks = RiskEngine().evaluate(self.snap(), BASE)
        self.assertEqual(risks["absent"].state, RiskState.NOT_APPLICABLE)
        self.assertEqual(risks["unknown"].state, RiskState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
