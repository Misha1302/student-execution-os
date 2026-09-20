from __future__ import annotations

from student_execution_os.domain.model import ActorCategory, CutoffState, Importance, UserTimeConstraintType
from student_execution_os.planning.model import (
    DisplayColour,
    DisplayProjection,
    FeasibilityStatus,
    NextAction,
    PlanBlockType,
    PlanSnapshot,
    PlanningSnapshot,
    RiskResult,
    RiskState,
)

_RISK_RANK = {
    RiskState.OVERDUE: 0,
    RiskState.IMPOSSIBLE: 1,
    RiskState.CRITICAL: 2,
    RiskState.AT_RISK: 3,
    RiskState.START_SOON: 4,
    RiskState.UNKNOWN: 5,
    RiskState.SAFE: 6,
    RiskState.NOT_APPLICABLE: 7,
}
_IMPORTANCE_RANK = {
    Importance.CRITICAL: 0,
    Importance.HIGH: 1,
    Importance.NORMAL: 2,
    Importance.LOW: 3,
}
_COLOUR = {
    RiskState.NOT_APPLICABLE: DisplayColour.TRANSPARENT,
    RiskState.SAFE: DisplayColour.GREEN,
    RiskState.START_SOON: DisplayColour.YELLOW,
    RiskState.UNKNOWN: DisplayColour.YELLOW,
    RiskState.AT_RISK: DisplayColour.RED,
    RiskState.CRITICAL: DisplayColour.RED,
    RiskState.IMPOSSIBLE: DisplayColour.BURNING,
    RiskState.OVERDUE: DisplayColour.BURNING,
}


def display_projection(snapshot: PlanningSnapshot, risks: dict[str, RiskResult]) -> tuple[DisplayProjection, ...]:
    result = []
    for task in snapshot.tasks:
        risk = risks.get(task.obligation.id)
        if risk is None:
            continue
        result.append(
            DisplayProjection(
                task.obligation.id,
                task.obligation.importance,
                risk.state,
                _COLOUR[risk.state],
            )
        )
    return tuple(sorted(result, key=lambda x: x.task_id))


def auto_order_task_ids(snapshot: PlanningSnapshot, risks: dict[str, RiskResult]) -> tuple[str, ...]:
    def key(task):
        risk = risks.get(task.obligation.id)
        risk_rank = _RISK_RANK[risk.state] if risk else 99
        if task.actual_cutoff.state is CutoffState.KNOWN and task.actual_cutoff.at is not None:
            relevant = (0, task.actual_cutoff.at)
        elif task.target_at is not None:
            relevant = (1, task.target_at)
        else:
            relevant = (2, task.obligation.created_at)
        return (
            risk_rank,
            *relevant,
            _IMPORTANCE_RANK[task.obligation.importance],
            task.obligation.created_at,
            task.obligation.id,
        )

    return tuple(t.obligation.id for t in sorted(snapshot.tasks, key=key))


def build_next_actions(
    snapshot: PlanningSnapshot,
    plan: PlanSnapshot,
    risks: dict[str, RiskResult],
) -> tuple[NextAction, ...]:
    if plan.feasibility_status is not FeasibilityStatus.FEASIBLE:
        return ()
    tasks = {t.obligation.id: t for t in snapshot.tasks}
    first_work = {}
    for block in sorted(plan.blocks):
        if block.type is PlanBlockType.WORK and block.obligation_id not in first_work:
            first_work[block.obligation_id] = block
    actions = []
    for task_id in auto_order_task_ids(snapshot, risks):
        block = first_work.get(task_id)
        task = tasks.get(task_id)
        risk = risks.get(task_id)
        if block is None or task is None or risk is None:
            continue
        relevant_at = (
            task.actual_cutoff.at
            if task.actual_cutoff.state is CutoffState.KNOWN
            else task.target_at
        )
        actions.append(
            NextAction(
                task_id=task_id,
                what=task.obligation.title,
                recommended_duration_minutes=block.duration_minutes,
                why_now=f"PLAN_START={block.starts_at.isoformat()};RISK={risk.state.value}",
                risk_if_skipped=f"CURRENT_RISK={risk.state.value}",
                relevant_at=relevant_at,
                plan_revision=plan.plan_revision,
            )
        )
        if len(actions) >= snapshot.policy.max_next_actions:
            break
    return tuple(actions)


def pin_work_block(
    repository,
    *,
    account_id: str,
    plan: PlanSnapshot,
    block_id: str,
    actor: ActorCategory,
    starts_at=None,
    ends_at=None,
):
    block = next((b for b in plan.blocks if b.id == block_id), None)
    if block is None or block.type is not PlanBlockType.WORK or block.obligation_id is None:
        raise ValueError("only a WORK PlanBlock can be pinned")
    if (starts_at is None) != (ends_at is None):
        raise ValueError("dragging a WORK block requires both starts_at and ends_at")
    target_start = block.starts_at if starts_at is None else starts_at
    target_end = block.ends_at if ends_at is None else ends_at
    if block.source_constraint_ids:
        constraint = repository.get_time_constraint(account_id, block.source_constraint_ids[0])
        return repository.update_time_constraint(
            account_id=account_id,
            constraint_id=constraint.id,
            expected_version=constraint.version,
            starts_at=target_start,
            ends_at=target_end,
            actor=actor,
            reason="Pinned from derived PlanBlock",
        )
    return repository.create_time_constraint(
        account_id=account_id,
        type=UserTimeConstraintType.PINNED_WORK,
        starts_at=target_start,
        ends_at=target_end,
        obligation_id=block.obligation_id,
        reason="Pinned from derived PlanBlock",
        actor=actor,
    )
