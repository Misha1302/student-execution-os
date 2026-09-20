from __future__ import annotations

from student_execution_os.domain.model import (
    AttendancePolicy,
    CutoffBoundary,
    CutoffState,
    DependencySuccessorKind,
    HalfOpenInterval,
    LifecycleStatus,
    MilestoneStatus,
    UserTimeConstraintType,
)
from student_execution_os.planning.model import PlanningSnapshot, WorkPlacement


def _ends_before_cutoff(task, end) -> bool:
    cutoff = task.actual_cutoff
    if cutoff.state is not CutoffState.KNOWN:
        return True
    assert cutoff.at is not None
    return end <= cutoff.at if cutoff.boundary is CutoffBoundary.INCLUSIVE else end < cutoff.at


def validate_witness(snapshot: PlanningSnapshot, witness: tuple[WorkPlacement, ...]) -> tuple[str, ...]:
    """Independently validate a claimed witness against the supported hard model."""
    errors: list[str] = []
    tasks = {task.obligation.id: task for task in snapshot.tasks if task.remaining_effort_minutes > 0}
    by_task: dict[str, list[WorkPlacement]] = {task_id: [] for task_id in tasks}

    for placement in witness:
        if placement.task_id not in tasks:
            errors.append(f"UNKNOWN_TASK_PLACEMENT:{placement.task_id}")
            continue
        by_task[placement.task_id].append(placement)
        if (
            placement.starts_at < snapshot.analysis_horizon_start
            or placement.ends_at > snapshot.analysis_horizon_end
        ):
            errors.append(f"PLACEMENT_OUTSIDE_ANALYSIS_HORIZON:{placement.task_id}")

    ordered = sorted(witness)
    for index, left in enumerate(ordered):
        left_interval = HalfOpenInterval(left.starts_at, left.ends_at)
        for right in ordered[index + 1 :]:
            if left_interval.overlaps(HalfOpenInterval(right.starts_at, right.ends_at)):
                errors.append(f"WORK_OVERLAP:{left.task_id}:{right.task_id}")

    required_events = [
        event
        for event in snapshot.events
        if event.attendance_policy is AttendancePolicy.REQUIRED
        and event.obligation.lifecycle_status is LifecycleStatus.ACTIVE
    ]
    hard_constraints = [
        constraint
        for constraint in snapshot.constraints
        if constraint.type is not UserTimeConstraintType.PINNED_WORK
    ]
    pins = {
        constraint.id: constraint
        for constraint in snapshot.constraints
        if constraint.type is UserTimeConstraintType.PINNED_WORK
    }

    for placement in witness:
        interval = HalfOpenInterval(placement.starts_at, placement.ends_at)
        if any(interval.overlaps(event.interval) for event in required_events):
            errors.append(f"WORK_OVERLAPS_REQUIRED_EVENT:{placement.task_id}")
        if any(interval.overlaps(constraint.interval) for constraint in hard_constraints):
            errors.append(f"WORK_OVERLAPS_CONSTRAINT:{placement.task_id}")
        if placement.source == "PINNED" and not any(
            constraint.obligation_id == placement.task_id and constraint.interval == interval
            for constraint in pins.values()
        ):
            errors.append(f"PINNED_WITNESS_WITHOUT_CONSTRAINT:{placement.task_id}")

    for task_id, task in tasks.items():
        placements = sorted(by_task.get(task_id, []), key=lambda p: (p.starts_at, p.ends_at))
        total = sum(placement.duration_minutes for placement in placements)
        if total != task.remaining_effort_minutes:
            errors.append(f"EFFORT_MISMATCH:{task_id}:{total}:{task.remaining_effort_minutes}")
            continue
        if task.actionable_from and any(p.starts_at < task.actionable_from for p in placements):
            errors.append(f"BEFORE_ACTIONABLE:{task_id}")
        for placement in placements:
            if not _ends_before_cutoff(task, placement.ends_at):
                errors.append(f"AFTER_CUTOFF:{task_id}")
        if not task.splittable:
            if len(placements) != 1 or (
                placements and placements[0].duration_minutes != task.remaining_effort_minutes
            ):
                errors.append(f"NON_SPLITTABLE_NOT_CONTIGUOUS:{task_id}")
        else:
            for index, placement in enumerate(placements):
                duration = placement.duration_minutes
                if task.max_chunk_minutes is not None and duration > task.max_chunk_minutes:
                    errors.append(f"MAX_CHUNK_VIOLATION:{task_id}")
                is_final = index == len(placements) - 1
                if (
                    task.min_chunk_minutes is not None
                    and duration < task.min_chunk_minutes
                    and not is_final
                ):
                    errors.append(f"NON_FINAL_MIN_CHUNK_VIOLATION:{task_id}")

    milestones = {
        milestone.id: milestone
        for milestone in snapshot.milestones
        if milestone.status is MilestoneStatus.ACTIVE
    }
    events = {event.obligation.id: event for event in required_events}
    for dependency in snapshot.dependencies:
        predecessor = by_task.get(dependency.predecessor_task_id, [])
        if not predecessor:
            continue
        predecessor_end = max(placement.ends_at for placement in predecessor)
        if dependency.successor_kind is DependencySuccessorKind.TASK:
            successor = by_task.get(dependency.successor_id, [])
            if successor and predecessor_end > min(p.starts_at for p in successor):
                errors.append(f"DEPENDENCY_ORDER_VIOLATION:{dependency.id}")
        elif (
            dependency.successor_kind is DependencySuccessorKind.EVENT
            and dependency.successor_id in events
        ):
            if predecessor_end > events[dependency.successor_id].interval.starts_at:
                errors.append(f"DEPENDENCY_EVENT_VIOLATION:{dependency.id}")
        elif dependency.successor_kind is DependencySuccessorKind.MILESTONE:
            milestone = milestones.get(dependency.successor_id)
            if (
                milestone is not None
                and milestone.hard_for_planning
                and predecessor_end > milestone.marker_at
            ):
                errors.append(f"DEPENDENCY_MILESTONE_VIOLATION:{dependency.id}")

    return tuple(sorted(set(errors)))
