from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from student_execution_os.domain.model import LifecycleStatus
from student_execution_os.planning.model import PlanningPolicy, PlanningSnapshot

_STABLE_CAPTURE_ATTEMPTS = 3


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _stable_payload(*, account_id, revision, analysis_start, analysis_end, output_start, output_end,
                    tasks, events, constraints, dependencies, milestones, policy) -> dict[str, object]:
    return {
        "account_id": account_id,
        "input_server_revision": revision,
        "analysis_horizon": [_iso(analysis_start), _iso(analysis_end)],
        "plan_output_horizon": [_iso(output_start), _iso(output_end)],
        "policy": {
            "version": policy.version,
            "minute_grid": policy.minute_grid,
            "start_soon_lead_minutes": policy.start_soon_lead_minutes,
            "max_next_actions": policy.max_next_actions,
        },
        "tasks": [
            {
                "id": t.obligation.id,
                "version": t.obligation.version,
                "status": t.obligation.lifecycle_status.value,
                "remaining": t.remaining_effort_minutes,
                "estimated_low": t.estimated_total_effort_low_minutes,
                "estimated_high": t.estimated_total_effort_high_minutes,
                "remaining_low": t.remaining_effort_low_minutes,
                "remaining_high": t.remaining_effort_high_minutes,
                "splittable": t.splittable,
                "min_chunk": t.min_chunk_minutes,
                "max_chunk": t.max_chunk_minutes,
                "actionable_from": _iso(t.actionable_from),
                "cutoff_state": t.actual_cutoff.state.value,
                "cutoff_at": _iso(t.actual_cutoff.at),
                "cutoff_boundary": t.actual_cutoff.boundary.value if t.actual_cutoff.boundary else None,
                "cutoff_precision": t.actual_cutoff.precision.value if t.actual_cutoff.precision else None,
                "target_at": _iso(t.target_at),
            }
            for t in tasks
        ],
        "events": [
            {
                "id": e.obligation.id,
                "version": e.obligation.version,
                "status": e.obligation.lifecycle_status.value,
                "starts_at": _iso(e.interval.starts_at),
                "ends_at": _iso(e.interval.ends_at),
                "attendance_policy": e.attendance_policy.value,
                "time_semantics": e.time_semantics.value,
                "location_effect": e.location_effect.kind.value,
            }
            for e in events
        ],
        "constraints": [
            {
                "id": c.id,
                "version": c.version,
                "type": c.type.value,
                "starts_at": _iso(c.interval.starts_at),
                "ends_at": _iso(c.interval.ends_at),
                "obligation_id": c.obligation_id,
            }
            for c in constraints
        ],
        "dependencies": [
            {
                "id": d.id,
                "predecessor_task_id": d.predecessor_task_id,
                "successor_kind": d.successor_kind.value,
                "successor_id": d.successor_id,
            }
            for d in dependencies
        ],
        "milestones": [
            {
                "id": m.id,
                "owner_kind": m.owner_kind.value,
                "owner_id": m.owner_id,
                "marker_at": _iso(m.marker_at),
                "role": m.role.value,
                "hard_for_planning": m.hard_for_planning,
                "status": m.status.value,
                "version": m.version,
            }
            for m in milestones
        ],
    }


def _read_stable_inputs(source, account_id: str):
    """Read one revision-consistent planning state without requiring source-specific transactions."""
    for _ in range(_STABLE_CAPTURE_ATTEMPTS):
        revision_before = source.get_server_revision(account_id)
        tasks = tuple(sorted(
            (t for t in source.list_tasks(account_id) if t.obligation.lifecycle_status is LifecycleStatus.ACTIVE),
            key=lambda t: t.obligation.id,
        ))
        events = tuple(sorted(
            (e for e in source.list_events(account_id) if e.obligation.lifecycle_status is LifecycleStatus.ACTIVE),
            key=lambda e: e.obligation.id,
        ))
        constraints = tuple(sorted(source.list_time_constraints(account_id), key=lambda c: c.id))
        dependencies = tuple(sorted(source.list_dependencies(account_id), key=lambda d: d.id))
        milestones = tuple(sorted(source.list_milestones(account_id), key=lambda m: m.id))
        revision_after = source.get_server_revision(account_id)
        if revision_before == revision_after:
            return (
                revision_after,
                tasks,
                events,
                constraints,
                dependencies,
                milestones,
            )
    raise RuntimeError("planning state changed during snapshot capture")


def build_planning_snapshot(
    source,
    *,
    account_id: str,
    analysis_horizon_start: datetime,
    analysis_horizon_end: datetime,
    plan_output_horizon_start: datetime | None = None,
    plan_output_horizon_end: datetime | None = None,
    policy: PlanningPolicy | None = None,
) -> PlanningSnapshot:
    """Materialize one immutable, revision-bound planning input.

    The builder retries if canonical state changes while tables are being read,
    preventing a mixed-revision snapshot from being labelled with one committed
    server revision. It also extends the analysis horizon through every known
    active hard cutoff instead of confusing the shorter display horizon with
    feasibility.
    """
    policy = policy or PlanningPolicy()
    (
        revision,
        tasks,
        events,
        constraints,
        dependencies,
        milestones,
    ) = _read_stable_inputs(source, account_id)

    known_cutoffs = [t.actual_cutoff.at for t in tasks if t.actual_cutoff.at is not None]
    effective_analysis_end = max([analysis_horizon_end, *known_cutoffs]) if known_cutoffs else analysis_horizon_end
    output_start = plan_output_horizon_start or analysis_horizon_start
    output_end = plan_output_horizon_end or min(analysis_horizon_end, effective_analysis_end)
    if output_end > effective_analysis_end:
        raise ValueError("plan output horizon cannot extend beyond analysis horizon")

    payload = _stable_payload(
        account_id=account_id,
        revision=revision,
        analysis_start=analysis_horizon_start,
        analysis_end=effective_analysis_end,
        output_start=output_start,
        output_end=output_end,
        tasks=tasks,
        events=events,
        constraints=constraints,
        dependencies=dependencies,
        milestones=milestones,
        policy=policy,
    )
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return PlanningSnapshot(
        account_id=account_id,
        input_server_revision=revision,
        input_hash=input_hash,
        analysis_horizon_start=analysis_horizon_start,
        analysis_horizon_end=effective_analysis_end,
        plan_output_horizon_start=output_start,
        plan_output_horizon_end=output_end,
        tasks=tasks,
        events=events,
        constraints=constraints,
        dependencies=dependencies,
        milestones=milestones,
        policy=policy,
    )
