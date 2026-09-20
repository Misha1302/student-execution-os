from __future__ import annotations

import hashlib
from dataclasses import dataclass

from student_execution_os.domain.clock import Clock, SystemClock
from student_execution_os.domain.model import AttendancePolicy, CutoffState, Importance, LifecycleStatus, UserTimeConstraintType
from student_execution_os.planning.feasibility import FeasibilityEngine
from student_execution_os.planning.model import (
    FeasibilityStatus,
    PlanBlock,
    PlanBlockType,
    PlanSnapshot,
    PlanningSnapshot,
)

_IMPORTANCE_RANK = {
    Importance.CRITICAL: 0,
    Importance.HIGH: 1,
    Importance.NORMAL: 2,
    Importance.LOW: 3,
}


def _block_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass(frozen=True)
class Planner:
    clock: Clock = SystemClock()
    exact_search_enabled: bool = True
    node_limit: int = 200_000
    timeout_seconds: float = 2.0

    def plan(self, snapshot: PlanningSnapshot, previous_plan: PlanSnapshot | None = None) -> PlanSnapshot:
        def priority(task):
            if task.target_at is not None:
                temporal = (0, task.target_at)
            elif task.actual_cutoff.state is CutoffState.KNOWN:
                temporal = (1, task.actual_cutoff.at)
            else:
                temporal = (2, task.obligation.created_at)
            return (*temporal, _IMPORTANCE_RANK[task.obligation.importance], task.obligation.id)

        feasibility = FeasibilityEngine(
            exact_search_enabled=self.exact_search_enabled,
            node_limit=self.node_limit,
            timeout_seconds=self.timeout_seconds,
            task_tie_break=priority,
        ).evaluate(snapshot)

        blocks: list[PlanBlock] = []
        for event in snapshot.events:
            if (
                event.obligation.lifecycle_status is LifecycleStatus.ACTIVE
                and event.attendance_policy is AttendancePolicy.REQUIRED
                and event.interval.starts_at < snapshot.plan_output_horizon_end
                and snapshot.plan_output_horizon_start < event.interval.ends_at
            ):
                blocks.append(PlanBlock(
                    starts_at=event.interval.starts_at,
                    ends_at=event.interval.ends_at,
                    id=_block_id(snapshot.input_hash, "EVENT", event.obligation.id, event.interval.starts_at, event.interval.ends_at),
                    type=PlanBlockType.EVENT_PROJECTION,
                    obligation_id=event.obligation.id,
                    source_event_id=event.obligation.id,
                    explanation="REQUIRED_EVENT_PROJECTION",
                ))

        for transition in snapshot.travel_projection.transitions:
            if (
                transition.travel_interval.starts_at < snapshot.plan_output_horizon_end
                and snapshot.plan_output_horizon_start < transition.travel_interval.ends_at
            ):
                blocks.append(
                    PlanBlock(
                        starts_at=transition.travel_interval.starts_at,
                        ends_at=transition.travel_interval.ends_at,
                        id=_block_id(
                            snapshot.input_hash,
                            "TRAVEL",
                            transition.target_event_id,
                            transition.travel_estimate_id,
                            transition.travel_interval.starts_at,
                            transition.travel_interval.ends_at,
                        ),
                        type=PlanBlockType.TRAVEL_TRANSITION,
                        source_event_id=transition.target_event_id,
                        travel_estimate_id=transition.travel_estimate_id,
                        explanation="REQUIRED_TRAVEL_TRANSITION",
                    )
                )
            if (
                transition.arrival_buffer is not None
                and transition.arrival_buffer.starts_at < snapshot.plan_output_horizon_end
                and snapshot.plan_output_horizon_start < transition.arrival_buffer.ends_at
            ):
                blocks.append(
                    PlanBlock(
                        starts_at=transition.arrival_buffer.starts_at,
                        ends_at=transition.arrival_buffer.ends_at,
                        id=_block_id(
                            snapshot.input_hash,
                            "BUFFER",
                            transition.target_event_id,
                            transition.travel_estimate_id,
                            transition.arrival_buffer.starts_at,
                            transition.arrival_buffer.ends_at,
                        ),
                        type=PlanBlockType.BUFFER,
                        source_event_id=transition.target_event_id,
                        travel_estimate_id=transition.travel_estimate_id,
                        explanation="HARD_ARRIVAL_BUFFER",
                    )
                )

        if feasibility.status is FeasibilityStatus.FEASIBLE:
            pins = [c for c in snapshot.constraints if c.type is UserTimeConstraintType.PINNED_WORK]
            for placement in feasibility.witness:
                if not (
                    placement.starts_at < snapshot.plan_output_horizon_end
                    and snapshot.plan_output_horizon_start < placement.ends_at
                ):
                    continue
                source_ids = tuple(sorted(
                    c.id for c in pins
                    if c.obligation_id == placement.task_id
                    and c.interval.starts_at == placement.starts_at
                    and c.interval.ends_at == placement.ends_at
                ))
                blocks.append(PlanBlock(
                    starts_at=placement.starts_at,
                    ends_at=placement.ends_at,
                    id=_block_id(snapshot.input_hash, "WORK", placement.task_id, placement.starts_at, placement.ends_at),
                    type=PlanBlockType.WORK,
                    obligation_id=placement.task_id,
                    source_constraint_ids=source_ids,
                    explanation=("PINNED_WORK_PROJECTION" if source_ids else "VERIFIED_FEASIBILITY_WITNESS"),
                ))

        plan_id = hashlib.sha256(("plan|" + snapshot.input_hash).encode("utf-8")).hexdigest()[:32]
        explanations = list(feasibility.reasons)
        if previous_plan is not None and not previous_plan.is_current_for(snapshot):
            explanations.append("REPLAN_INPUT_CHANGED")
        return PlanSnapshot(
            id=plan_id,
            account_id=snapshot.account_id,
            plan_revision=f"{snapshot.input_server_revision}:{snapshot.input_hash[:12]}",
            input_server_revision=snapshot.input_server_revision,
            input_hash=snapshot.input_hash,
            horizon_start=snapshot.plan_output_horizon_start,
            horizon_end=snapshot.plan_output_horizon_end,
            feasibility_status=feasibility.status,
            generated_at=self.clock.now(),
            blocks=tuple(sorted(blocks)),
            explanations=tuple(explanations),
        )