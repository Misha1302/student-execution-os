from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.planning.actions import build_next_actions, display_projection
from student_execution_os.planning.model import DisplayProjection, NextAction, PlanSnapshot, PlanningSnapshot, RiskResult
from student_execution_os.planning.planner import Planner
from student_execution_os.planning.risk import RiskEngine


@dataclass(frozen=True)
class PlanningOutcome:
    plan: PlanSnapshot
    risks: tuple[RiskResult, ...]
    display: tuple[DisplayProjection, ...]
    next_actions: tuple[NextAction, ...]


@dataclass(frozen=True)
class PlanningService:
    node_limit: int = 200_000
    timeout_seconds: float = 2.0

    def build(self, snapshot: PlanningSnapshot, *, now: datetime, previous_plan: PlanSnapshot | None = None) -> PlanningOutcome:
        planner = Planner(
            clock=FrozenClock(now),
            node_limit=self.node_limit,
            timeout_seconds=self.timeout_seconds,
        )
        plan = planner.plan(snapshot, previous_plan=previous_plan)
        risk_map = RiskEngine(node_limit=self.node_limit, timeout_seconds=self.timeout_seconds).evaluate(snapshot, now)
        displays = display_projection(snapshot, risk_map)
        actions = build_next_actions(snapshot, plan, risk_map)
        return PlanningOutcome(plan, tuple(sorted(risk_map.values(), key=lambda r: r.task_id)), displays, actions)
