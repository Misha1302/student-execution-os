"""Values and parsers shared by the sync envelope and every command handler."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    CutoffBoundary,
    CutoffState,
    HardCutoff,
    LifecycleStatus,
    TemporalPrecision,
)

APPLIED, NOOP, CONFLICT, REJECTED = "APPLIED", "NOOP", "CONFLICT", "REJECTED"
OPEN = {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")


@dataclass(frozen=True)
class Outcome:
    status: str
    entity: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None


def parse_instant(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a UTC offset")
    # The planner works on whole minutes.
    return parsed.replace(second=0, microsecond=0)


def parse_cutoff(payload: Any) -> HardCutoff:
    if payload is None:
        return HardCutoff.unknown()
    if not isinstance(payload, dict):
        raise ValidationError("actual_cutoff must be an object")
    state = CutoffState(payload.get("state", "UNKNOWN"))
    if state is CutoffState.ABSENT:
        return HardCutoff.absent()
    if state is CutoffState.UNKNOWN:
        return HardCutoff.unknown(TemporalPrecision(payload.get("precision", TemporalPrecision.UNKNOWN.value)))
    at = parse_instant(payload.get("at"), "actual_cutoff.at")
    if at is None:
        raise ValidationError("a KNOWN deadline requires at")
    return HardCutoff.known(at, CutoffBoundary(payload.get("boundary", CutoffBoundary.INCLUSIVE.value)))


def _minutes(value: Any, field: str, *, allow_zero: bool = False, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise ValidationError(f"{field} is required")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number of minutes") from exc
    if number < 0 or (number == 0 and not allow_zero) or number > 100_000:
        raise ValidationError(f"{field} is out of range")
    return number


def _title(value: Any) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ValidationError("title is required")
    if len(title) > 300:
        raise ValidationError("title is longer than 300 characters")
    return title


def _description(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) > 5000:
        raise ValidationError("description is longer than 5000 characters")
    return text or None


def _short_text(value: Any, field: str) -> str | None:
    text = str(value or "").strip()
    if len(text) > 500:
        raise ValidationError(f"{field} is longer than 500 characters")
    return text or None


def _override_json(item) -> dict[str, Any]:
    return {
        "id": item.id, "layer": item.layer.value, "action": item.action.value,
        "replacement_start_local": None if item.replacement_start_local is None else item.replacement_start_local.isoformat(),
        "replacement_duration_minutes": item.replacement_duration_minutes, "title": item.replacement_title,
        "location_text": item.location_text, "teacher": item.teacher, "note": item.note,
        "reason": None if item.reason is None else item.reason.value, "version": item.version,
    }


def _series_json(template) -> dict[str, Any]:
    return {
        "kind": "SERIES", "id": template.id, "title": template.title, "category": template.category.value,
        "dtstart_local": template.dtstart_local.isoformat(), "duration_minutes": template.duration_minutes,
        "recurrence_rule": template.recurrence_rule.canonical(), "timezone_name": template.timezone_name,
        "location_text": template.location_text, "teacher": template.teacher,
        "source_system_id": template.source_system_id, "version": template.version,
        "series_end_before_local": None if template.series_end_before_local is None else template.series_end_before_local.isoformat(),
    }
