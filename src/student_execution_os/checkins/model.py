"""Tracked check-ins: small repeated actions whose actual outcome matters.

    CheckInTemplate ──recurrence──▶ CheckInOccurrence ──user / policy──▶ outcome

A check-in is not a Task. It is never placed by the planner as work and never
consumes planner capacity by itself; a QUOTA check-in only *reports* its remaining
quantity (and, when the user gave one, an effort per unit) as planning context.

The occurrence is the single owner of what happened. Reminders draw attention to
it (``reminders`` rows, ADR 0019/0034); snoozing a reminder never changes the
outcome, and closing a reminder never records one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import require_aware
from student_execution_os.recurrence import RecurrenceRule


class CheckInKind(StrEnum):
    ROUTINE = "ROUTINE"        # "вынести мусор", "полить цветы"
    MEDICATION = "MEDICATION"  # adherence tracking only; dose/instructions are user text
    QUOTA = "QUOTA"            # "20 задач в день": quantity, not a boolean


class OccurrenceStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"      # the user said it will not / did not happen
    MISSED = "MISSED"        # the observation window passed without an answer (policy)
    CANCELLED = "CANCELLED"  # removed from the schedule for that day (not counted)


RESOLVED = frozenset({OccurrenceStatus.DONE, OccurrenceStatus.SKIPPED, OccurrenceStatus.MISSED,
                      OccurrenceStatus.CANCELLED})
DELIVERIES = ("PUSH", "ALARM", "PUSH_AND_ALARM")


@dataclass(frozen=True)
class CheckInTemplate:
    id: str
    account_id: str
    kind: CheckInKind
    title: str
    dose_text: str | None
    instructions: str | None
    target_quantity: int | None
    unit: str | None
    unit_effort_seconds: int | None
    dtstart_local: datetime
    recurrence_rule: RecurrenceRule
    timezone_name: str
    remind: bool
    delivery: str
    followup_minutes: int | None
    window_minutes: int | None
    status: str
    series_end_before_local: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip() or not self.timezone_name:
            raise ValidationError("check-in identity/title/timezone are required")
        if self.dtstart_local.tzinfo is not None:
            raise ValidationError("check-in DTSTART must be local civil time")
        if (self.kind is CheckInKind.QUOTA) != (self.target_quantity is not None):
            raise ValidationError("a QUOTA check-in (and only it) has a target quantity")
        if self.kind is not CheckInKind.QUOTA and (self.unit or self.unit_effort_seconds):
            raise ValidationError("unit and effort per unit belong to QUOTA check-ins")
        if self.kind is not CheckInKind.MEDICATION and (self.dose_text or self.instructions):
            raise ValidationError("dose and instructions belong to MEDICATION check-ins")
        if self.delivery not in DELIVERIES:
            raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
        if self.status not in {"ACTIVE", "ENDED"}:
            raise ValidationError("invalid check-in status")
        if self.version < 1:
            raise ValidationError("check-in version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class CheckInOccurrence:
    account_id: str
    template_id: str
    original_recurrence_id: str
    moved_to_local: datetime | None
    status: OccurrenceStatus
    resolved_by: str | None
    occurred_at: datetime | None
    acted_at: datetime | None
    quantity_done: int
    target_quantity: int | None
    note: str | None
    reminder_id: str | None
    followups_sent: int
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not all((self.account_id, self.template_id, self.original_recurrence_id)):
            raise ValidationError("check-in occurrence identity is required")
        if (self.status is OccurrenceStatus.PENDING) != (self.resolved_by is None):
            raise ValidationError("only a resolved occurrence names who resolved it")
        if self.moved_to_local is not None and self.moved_to_local.tzinfo is not None:
            raise ValidationError("a moved occurrence keeps local civil time")

    @property
    def identity(self) -> tuple[str, str]:
        return self.template_id, self.original_recurrence_id

    @property
    def scheduled_local(self) -> datetime:
        return self.moved_to_local or datetime.fromisoformat(self.original_recurrence_id)
