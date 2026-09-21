from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    AttendancePolicy,
    Importance,
    LocationEffect,
    ObligationCategory,
    require_aware,
)


class RecurrenceFrequency(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class LocalTimeResolutionPolicy(StrEnum):
    """Deterministic local-civil-time resolution for DST edge cases.

    Ambiguous local times use the earlier fold. Nonexistent local times shift
    forward to the first valid minute. The original recurrence identity is
    never rewritten by that resolution.
    """

    EARLIER_FOLD_SHIFT_FORWARD = "EARLIER_FOLD_SHIFT_FORWARD"


class OccurrenceOverrideAction(StrEnum):
    CANCEL = "CANCEL"
    MODIFY = "MODIFY"


@dataclass(frozen=True)
class RecurrenceRule:
    frequency: RecurrenceFrequency
    interval: int = 1
    count: int | None = None
    until_local: datetime | None = None

    def __post_init__(self) -> None:
        if self.interval < 1:
            raise ValidationError("recurrence interval must be >= 1")
        if self.count is not None and self.count < 1:
            raise ValidationError("recurrence count must be >= 1")
        if self.until_local is not None and self.until_local.tzinfo is not None:
            raise ValidationError("recurrence UNTIL local value must be naive local civil time")

    @classmethod
    def parse(cls, value: str) -> "RecurrenceRule":
        if not value or not value.strip():
            raise ValidationError("recurrence_rule is required")
        parts: dict[str, str] = {}
        for raw in value.strip().upper().split(";"):
            if "=" not in raw:
                raise ValidationError("invalid RRULE component")
            key, item = raw.split("=", 1)
            if not key or key in parts:
                raise ValidationError("invalid or duplicate RRULE component")
            parts[key] = item
        unsupported = set(parts) - {"FREQ", "INTERVAL", "COUNT", "UNTIL"}
        if unsupported:
            raise ValidationError(f"unsupported RRULE components: {','.join(sorted(unsupported))}")
        if "FREQ" not in parts:
            raise ValidationError("RRULE requires FREQ")
        try:
            frequency = RecurrenceFrequency(parts["FREQ"])
        except ValueError as exc:
            raise ValidationError("only DAILY and WEEKLY RRULE frequencies are supported") from exc
        interval = int(parts.get("INTERVAL", "1"))
        count = int(parts["COUNT"]) if "COUNT" in parts else None
        until_local = datetime.fromisoformat(parts["UNTIL"]) if "UNTIL" in parts else None
        return cls(frequency=frequency, interval=interval, count=count, until_local=until_local)

    def canonical(self) -> str:
        items = [f"FREQ={self.frequency.value}"]
        if self.interval != 1:
            items.append(f"INTERVAL={self.interval}")
        if self.count is not None:
            items.append(f"COUNT={self.count}")
        if self.until_local is not None:
            items.append(f"UNTIL={self.until_local.isoformat()}")
        return ";".join(items)


@dataclass(frozen=True)
class RecurringTemplate:
    id: str
    account_id: str
    title: str
    description: str | None
    category: ObligationCategory
    importance: Importance
    dtstart_local: datetime
    duration_minutes: int
    recurrence_rule: RecurrenceRule
    timezone_name: str
    attendance_policy: AttendancePolicy
    location_effect: LocationEffect
    arrival_requirement_minutes: int
    resolution_policy: LocalTimeResolutionPolicy
    series_end_before_local: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip() or not self.timezone_name:
            raise ValidationError("recurring template identity/title/timezone are required")
        if self.dtstart_local.tzinfo is not None:
            raise ValidationError("recurring DTSTART must retain naive local civil semantics")
        if self.series_end_before_local is not None and self.series_end_before_local.tzinfo is not None:
            raise ValidationError("series split boundary must be local civil time")
        if self.duration_minutes <= 0:
            raise ValidationError("recurring duration must be positive")
        if self.arrival_requirement_minutes < 0:
            raise ValidationError("arrival requirement cannot be negative")
        if self.version < 1:
            raise ValidationError("recurring template version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class OccurrenceOverride:
    id: str
    account_id: str
    template_id: str
    original_recurrence_id: str
    action: OccurrenceOverrideAction
    replacement_start_local: datetime | None
    replacement_duration_minutes: int | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not all((self.id, self.account_id, self.template_id, self.original_recurrence_id)):
            raise ValidationError("occurrence override identity is required")
        if self.replacement_start_local is not None and self.replacement_start_local.tzinfo is not None:
            raise ValidationError("replacement start must retain local civil semantics")
        if self.action is OccurrenceOverrideAction.CANCEL:
            if self.replacement_start_local is not None or self.replacement_duration_minutes is not None:
                raise ValidationError("CANCEL override cannot carry replacement fields")
        elif self.replacement_start_local is None:
            raise ValidationError("MODIFY override requires replacement_start_local")
        if self.replacement_duration_minutes is not None and self.replacement_duration_minutes <= 0:
            raise ValidationError("replacement duration must be positive")
        if self.version < 1:
            raise ValidationError("occurrence override version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class RecurringOccurrence:
    template_id: str
    original_recurrence_id: str
    starts_at: datetime
    ends_at: datetime
    cancelled: bool
    override_id: str | None = None

    def __post_init__(self) -> None:
        if not self.template_id or not self.original_recurrence_id:
            raise ValidationError("recurring occurrence identity is required")
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if self.starts_at >= self.ends_at:
            raise ValidationError("recurring occurrence requires starts_at < ends_at")

    @property
    def identity(self) -> tuple[str, str]:
        return (self.template_id, self.original_recurrence_id)
