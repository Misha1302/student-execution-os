from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import math

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


def _calibrated_snapshot(snapshot: PlanningSnapshot) -> PlanningSnapshot:
    multipliers = dict(snapshot.effort_multipliers)
    if not multipliers:
        return snapshot
    tasks = []
    for task in snapshot.tasks:
        multiplier = float(multipliers.get(task.obligation.category.value, 1.0))
        if multiplier <= 1.0 or task.remaining_effort_minutes is None:
            tasks.append(task)
            continue
        scale = lambda value: None if value is None else int(math.ceil(value * multiplier))
        scaled_remaining = scale(task.remaining_effort_minutes)
        max_chunk = task.max_chunk_minutes
        if not task.splittable and scaled_remaining is not None:
            max_chunk = max(max_chunk or 0, scaled_remaining)
        tasks.append(replace(
            task,
            estimated_total_effort_minutes=scale(task.estimated_total_effort_minutes),
            remaining_effort_minutes=scaled_remaining,
            estimated_total_effort_low_minutes=scale(task.estimated_total_effort_low_minutes),
            estimated_total_effort_high_minutes=scale(task.estimated_total_effort_high_minutes),
            remaining_effort_low_minutes=scale(task.remaining_effort_low_minutes),
            remaining_effort_high_minutes=scale(task.remaining_effort_high_minutes),
            max_chunk_minutes=max_chunk,
        ))
    return replace(snapshot, tasks=tuple(tasks))


@dataclass(frozen=True)
class PlanningService:
    node_limit: int = 200_000
    timeout_seconds: float = 2.0

    def build(self, snapshot: PlanningSnapshot, *, now: datetime, previous_plan: PlanSnapshot | None = None) -> PlanningOutcome:
        effective = _calibrated_snapshot(snapshot)
        planner = Planner(
            clock=FrozenClock(now),
            node_limit=self.node_limit,
            timeout_seconds=self.timeout_seconds,
        )
        plan = planner.plan(effective, previous_plan=previous_plan)
        if snapshot.effort_multipliers:
            plan = replace(plan, explanations=tuple(plan.explanations) + tuple(
                f"CALIBRATION:{category}:{multiplier:.2f}"
                for category, multiplier in snapshot.effort_multipliers if multiplier > 1.0
            ))
        risk_map = RiskEngine(node_limit=self.node_limit, timeout_seconds=self.timeout_seconds).evaluate(effective, now)
        displays = display_projection(effective, risk_map)
        actions = build_next_actions(effective, plan, risk_map)
        return PlanningOutcome(plan, tuple(sorted(risk_map.values(), key=lambda r: r.task_id)), displays, actions)
