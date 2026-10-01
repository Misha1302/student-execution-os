from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import ValidationError


class TemporalPrecision(StrEnum):
    EXACT = "EXACT"
    APPROXIMATE = "APPROXIMATE"


class TemporalTransformKind(StrEnum):
    RELATIVE_SHIFT = "RELATIVE_SHIFT"
    SHIFT_WITH_GUARD_AND_FALLBACK = "SHIFT_WITH_GUARD_AND_FALLBACK"


@dataclass(frozen=True)
class ResolvedTemporalChange:
    when: datetime
    precision: TemporalPrecision
    reason: str


def _clock(value: object, field: str) -> time:
    if not isinstance(value, str):
        raise ValidationError(f"assistant {field} must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"assistant {field} must be HH:MM") from None
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ValidationError(f"assistant {field} must be HH:MM")
    return parsed


def _keys(value: object, allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValidationError(f"assistant {field} has unsupported fields")
    return value


def _minutes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -525_600 <= value <= 525_600 or value == 0:
        raise ValidationError("assistant temporal delta_minutes must be a non-zero whole number")
    return value


def _next_day(day: date) -> date:
    return day + timedelta(days=1)


def resolve_temporal_transform(
    raw: object,
    *,
    current: datetime,
    timezone_name: str,
) -> ResolvedTemporalChange:
    transform = _keys(raw, {"kind", "delta_minutes", "guard", "fallback"}, "temporal_transform")
    try:
        kind = TemporalTransformKind(transform.get("kind"))
    except ValueError:
        raise ValidationError("assistant temporal_transform has an invalid kind") from None
    delta = _minutes(transform.get("delta_minutes"))
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError):
        raise ValidationError("assistant temporal resolver has an invalid timezone") from None
    local_current = current.astimezone(zone)
    candidate = local_current + timedelta(minutes=delta)
    if kind is TemporalTransformKind.RELATIVE_SHIFT:
        if "guard" in transform or "fallback" in transform:
            raise ValidationError("assistant relative shift cannot contain guard or fallback")
        return ResolvedTemporalChange(candidate, TemporalPrecision.EXACT, "RELATIVE_SHIFT")

    guard = _keys(transform.get("guard"), {"not_after_local_time"}, "temporal guard")
    latest = _clock(guard.get("not_after_local_time"), "guard.not_after_local_time")
    if candidate.timetz().replace(tzinfo=None) <= latest:
        return ResolvedTemporalChange(candidate, TemporalPrecision.EXACT, "GUARD_SATISFIED")

    fallback = _keys(
        transform.get("fallback"),
        {"relative_day", "preferred_local_time", "precision"},
        "temporal fallback",
    )
    if fallback.get("relative_day") != "NEXT_MORNING":
        raise ValidationError("assistant temporal fallback relative_day must be NEXT_MORNING")
    preferred = _clock(fallback.get("preferred_local_time"), "fallback.preferred_local_time")
    try:
        precision = TemporalPrecision(fallback.get("precision"))
    except ValueError:
        raise ValidationError("assistant temporal fallback has an invalid precision") from None
    resolved = datetime.combine(_next_day(local_current.date()), preferred, zone)
    return ResolvedTemporalChange(resolved, precision, "GUARD_TRIGGERED_FALLBACK")
