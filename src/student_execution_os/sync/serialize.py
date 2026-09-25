"""JSON views of canonical entities shared by the REST read models and sync."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: jsonify(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [jsonify(v) for v in value]
    return value


def task_payload(task, *, risk=None, effective=None, remind_at: datetime | None = None) -> dict[str, Any]:
    cutoff = task.actual_cutoff
    ob = task.obligation
    return {
        "kind": "TASK",
        "id": ob.id,
        "title": ob.title,
        "description": ob.description,
        "category": ob.category.value,
        "importance": ob.importance.value,
        "status": ob.lifecycle_status.value,
        "version": ob.version,
        "created_at": jsonify(ob.created_at),
        "updated_at": jsonify(ob.updated_at),
        "completed_at": jsonify(ob.completed_at),
        "estimated_total_effort_minutes": task.estimated_total_effort_minutes,
        "estimated_total_effort_low_minutes": task.estimated_total_effort_low_minutes,
        "estimated_total_effort_high_minutes": task.estimated_total_effort_high_minutes,
        "remaining_effort_minutes": task.remaining_effort_minutes,
        "remaining_effort_low_minutes": task.remaining_effort_low_minutes,
        "remaining_effort_high_minutes": task.remaining_effort_high_minutes,
        "splittable": task.splittable,
        "min_chunk_minutes": task.min_chunk_minutes,
        "max_chunk_minutes": task.max_chunk_minutes,
        "actionable_from": jsonify(task.actionable_from),
        "target_at": jsonify(task.target_at),
        "started_at": jsonify(task.started_at),
        "last_progress_at": jsonify(task.last_progress_at),
        # A reminder the user asked for that has not gone out yet (reminder state, not a task field).
        "remind_at": jsonify(remind_at),
        "actual_cutoff": {
            "state": cutoff.state.value,
            "at": jsonify(cutoff.at),
            "boundary": cutoff.boundary.value if cutoff.boundary else None,
            "precision": cutoff.precision.value if cutoff.precision else None,
        },
        "risk": None if risk is None else {
            "state": risk.state.value,
            "basis": risk.basis.value,
            "reasons": list(risk.reasons),
            "latest_safe_start": jsonify(risk.latest_safe_start),
            "policy_version": risk.policy_version,
        },
        "cutoff_truth": None if effective is None else {
            "state": effective.state.value,
            "evidence_ids": list(effective.evidence_ids),
            "policy_version": effective.policy_version,
            "reason": effective.reason,
            "conflict_id": effective.conflict_id,
            "override_id": effective.override_id,
            "planning_projection": jsonify(effective.planning_projection),
            "admissible_cutoffs": jsonify(effective.admissible_cutoffs),
        },
    }


def event_payload(event) -> dict[str, Any]:
    ob = event.obligation
    return {
        "kind": "EVENT",
        "id": ob.id,
        "title": ob.title,
        "description": ob.description,
        "category": ob.category.value,
        "importance": ob.importance.value,
        "status": ob.lifecycle_status.value,
        "version": ob.version,
        "starts_at": jsonify(event.interval.starts_at),
        "ends_at": jsonify(event.interval.ends_at),
        "time_semantics": event.time_semantics.value,
        "attendance_policy": event.attendance_policy.value,
        "location_effect": {
            "kind": event.location_effect.kind.value,
            "origin_place_id": event.location_effect.origin_place_id,
            "destination_place_id": event.location_effect.destination_place_id,
        },
        "location_options": [{
            "id": option.id, "label": option.label,
            "location_effect": {"kind": option.effect.kind.value,
                                "origin_place_id": option.effect.origin_place_id,
                                "destination_place_id": option.effect.destination_place_id},
        } for option in event.location_options],
        "selected_location_option_id": event.selected_location_option_id,
        "arrival_requirement_minutes": event.arrival_requirement_minutes,
        "canonical": True,
    }
