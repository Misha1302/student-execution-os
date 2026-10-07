from __future__ import annotations

import hashlib
from time import monotonic
from dataclasses import dataclass, replace

from student_execution_os.domain.clock import Clock, SystemClock
from datetime import datetime, timedelta

from student_execution_os.domain.model import (
    AttendancePolicy,
    CutoffState,
    HardCutoff,
    Importance,
    LifecycleStatus,
    Obligation,
    ObligationCategory,
    ObligationKind,
    Task,
    UserTimeConstraintType,
)
from student_execution_os.planning.feasibility import FeasibilityEngine
from student_execution_os.planning.preferences import PreferenceGuide, relaxation_order
from student_execution_os.planning.model import (
    FeasibilityStatus,
    PlanBlock,
    PlanBlockType,
    PlanSnapshot,
    PlanningSnapshot,
    QuotaDemand,
)

_IMPORTANCE_RANK = {
    Importance.CRITICAL: 0,
    Importance.HIGH: 1,
    Importance.NORMAL: 2,
    Importance.LOW: 3,
}


# Wall-clock budget of the preference pass; past it the hard plan is kept as is.
PREFERENCE_BUDGET_SECONDS = 0.75


class _PreferenceBudgetExhausted(Exception):
    pass


def _block_id(*parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


QUOTA_DEMAND_PREFIX = "quota-demand:"
QUOTA_MIN_CHUNK = 15
QUOTA_MAX_CHUNK = 60


def _minute_up(value: datetime) -> datetime:
    floor = value.replace(second=0, microsecond=0)
    return floor if floor == value else floor + timedelta(minutes=1)


def _quota_work(snapshot: PlanningSnapshot) -> dict[str, tuple[Task, QuotaDemand]]:
    """Each open quota's remaining time as engine-only work, keyed by its work id.

    These exist only inside one planner run so the feasibility engine can reserve the
    time; they never leave the planner as Tasks (their placements become QUOTA blocks).
    """
    out: dict[str, tuple[Task, QuotaDemand]] = {}
    for demand in snapshot.quota_demands:
        start = max(_minute_up(demand.available_from), snapshot.analysis_horizon_start.replace(second=0, microsecond=0))
        start = _minute_up(start)
        due = demand.due_by.replace(second=0, microsecond=0)
        if due > snapshot.analysis_horizon_end:
            due = snapshot.analysis_horizon_end.replace(second=0, microsecond=0)
        if due - start < timedelta(minutes=min(demand.remaining_minutes, QUOTA_MIN_CHUNK)):
            continue
        work_id = f"{QUOTA_DEMAND_PREFIX}{demand.template_id}:{demand.original_recurrence_id}"
        minutes = demand.remaining_minutes
        obligation = Obligation(
            id=work_id, account_id=snapshot.account_id, kind=ObligationKind.TASK,
            category=ObligationCategory.GENERAL, title=demand.title, description=None,
            lifecycle_status=LifecycleStatus.ACTIVE, importance=Importance.NORMAL,
            created_at=demand.available_from, updated_at=demand.available_from, completed_at=None, version=1,
        )
        task = Task(
            obligation=obligation, estimated_total_effort_minutes=minutes, remaining_effort_minutes=minutes,
            splittable=True, min_chunk_minutes=min(minutes, QUOTA_MIN_CHUNK), max_chunk_minutes=QUOTA_MAX_CHUNK,
            actionable_from=start, actual_cutoff=HardCutoff.known(due), target_at=None,
        )
        out[work_id] = (task, demand)
    return out


@dataclass(frozen=True)
class Planner:
    clock: Clock = SystemClock()
    exact_search_enabled: bool = True
    node_limit: int = 200_000
    timeout_seconds: float = 2.0

    def plan(self, snapshot: PlanningSnapshot, previous_plan: PlanSnapshot | None = None) -> PlanSnapshot:
        intent_rank = {task_id: index for index, task_id in enumerate(snapshot.soft_priority_task_ids)}
        def priority(task):
            intent = intent_rank.get(task.obligation.id, 99)
            if task.target_at is not None:
                return (0, task.target_at, intent, _IMPORTANCE_RANK[task.obligation.importance], task.obligation.id)
            if task.actual_cutoff.state is CutoffState.KNOWN:
                return (1, task.actual_cutoff.at, intent, _IMPORTANCE_RANK[task.obligation.importance], task.obligation.id)
            # With no timing signal, Daily Intent is the meaningful soft ordering
            # input. Creation time remains the deterministic fallback.
            return (2, intent, task.obligation.created_at, _IMPORTANCE_RANK[task.obligation.importance], task.obligation.id)

        engine = FeasibilityEngine(
            exact_search_enabled=self.exact_search_enabled,
            node_limit=self.node_limit,
            timeout_seconds=self.timeout_seconds,
            task_tie_break=priority,
        )
        # Open quotas with a pace are reserved as derived demand, but only while every
        # obligation still fits: a quota never makes the plan infeasible. When it would,
        # the plan is made without it and says which quota does not fit.
        quota_work = _quota_work(snapshot)
        quota_notes: tuple[str, ...] = ()
        planned = snapshot
        feasibility = None
        if quota_work:
            planned = replace(snapshot, tasks=snapshot.tasks + tuple(task for task, _ in quota_work.values()))
            feasibility = engine.evaluate(planned)
            if feasibility.status is not FeasibilityStatus.FEASIBLE:
                planned, feasibility = snapshot, None
        if feasibility is None:
            feasibility = engine.evaluate(snapshot)
            if quota_work and feasibility.status is FeasibilityStatus.FEASIBLE:
                quota_notes = tuple(sorted({f"QUOTA_DOES_NOT_FIT:{demand.template_id}" for _, demand in quota_work.values()}))
        preference_notes: tuple[str, ...] = ()
        if feasibility.status is FeasibilityStatus.FEASIBLE and snapshot.preference_windows and feasibility.witness:
            feasibility, preference_notes = self._honour_preferences(engine, planned, feasibility)

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

        quota_blocks: list[PlanBlock] = []
        if feasibility.status is FeasibilityStatus.FEASIBLE:
            pins = [c for c in snapshot.constraints if c.type is UserTimeConstraintType.PINNED_WORK]
            for placement in feasibility.witness:
                if not (
                    placement.starts_at < snapshot.plan_output_horizon_end
                    and snapshot.plan_output_horizon_start < placement.ends_at
                ):
                    continue
                if placement.task_id in quota_work:
                    demand = quota_work[placement.task_id][1]
                    quota_blocks.append(PlanBlock(
                        starts_at=placement.starts_at,
                        ends_at=placement.ends_at,
                        id=_block_id(snapshot.input_hash, "QUOTA", placement.task_id, placement.starts_at, placement.ends_at),
                        type=PlanBlockType.QUOTA,
                        explanation="DERIVED_QUOTA_DEMAND",
                        checkin_occurrence=(demand.template_id, demand.original_recurrence_id),
                    ))
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
        explanations = list(feasibility.reasons) + list(preference_notes) + list(quota_notes)
        for event in snapshot.events:
            if event.attendance_policy is AttendancePolicy.OPTIONAL and snapshot.policy.optional_event_policy in {
                "OMIT_OPTIONAL", "OMIT_OPTIONAL_AND_PREFERRED"
            }:
                explanations.append(f"OPTIONAL_EVENT_OMITTED:{event.obligation.id}")
            elif event.attendance_policy is AttendancePolicy.PREFERRED and snapshot.policy.optional_event_policy == "OMIT_OPTIONAL_AND_PREFERRED":
                explanations.append(f"PREFERRED_EVENT_OMITTED:{event.obligation.id}")
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
            quota_blocks=tuple(sorted(quota_blocks)),
        )

    @staticmethod
    def _honour_preferences(engine, snapshot, feasibility):
        """Re-place work so that soft preferences hold, relaxing them one by one if needed.

        The hard result's status is never changed: a preference can only pick a
        different legal witness. Every preference is reported as APPLIED, RELAXED or
        UNSATISFIABLE (hard facts already break it, e.g. an event fills the evening).
        """
        guide = PreferenceGuide(tuple(snapshot.preference_windows))
        if "EXACT_WITNESS" in feasibility.reasons:
            # Only the exact search found a legal plan: a constructive placement that is
            # even more constrained cannot exist, so nothing is honoured — say so.
            return feasibility, tuple(f"PREFERENCE_RELAXED:{pid}" for pid in guide.preference_ids)
        hard = engine.hard_occupancy(snapshot)
        if hard is None:
            return feasibility, ()
        occupied, pinned = hard
        broken = guide.unsatisfiable(occupied, pinned)
        active = guide.without(broken)
        relaxed: list[str] = []
        order = list(relaxation_order(active.windows))
        witness = None
        deadline = monotonic() + PREFERENCE_BUDGET_SECONDS

        def admissible(task, candidate, occupancy, placements):
            if monotonic() > deadline:
                raise _PreferenceBudgetExhausted
            return current.admissible(task, candidate, occupancy, placements)

        try:
            while True:
                current = active
                witness = engine.preferred_witness(snapshot, admissible) if active.windows else None
                if witness is not None or not order:
                    break
                dropped = order.pop(0)
                relaxed.append(dropped)
                active = active.without([dropped])
        except _PreferenceBudgetExhausted:
            # Placement preferences must never make planning slow: keep the hard plan.
            witness = None
            relaxed = [pid for pid in guide.preference_ids if pid not in broken]
        notes = [f"PREFERENCE_UNSATISFIABLE:{pid}" for pid in broken]
        notes += [f"PREFERENCE_RELAXED:{pid}" for pid in relaxed]
        if witness is None:
            return feasibility, tuple(notes)
        notes += [f"PREFERENCE_APPLIED:{pid}" for pid in active.preference_ids]
        return replace(feasibility, witness=witness), tuple(notes)
