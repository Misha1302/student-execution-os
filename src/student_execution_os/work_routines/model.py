from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import Importance, ObligationCategory, require_aware
from student_execution_os.recurrence import RecurrenceRule


@dataclass(frozen=True)
class WorkRoutineTemplate:
    id: str
    account_id: str
    title: str
    description: str | None
    category: ObligationCategory
    importance: Importance
    dtstart_local: datetime
    effort_minutes: int
    recurrence_rule: RecurrenceRule
    timezone_name: str
    splittable: bool
    min_chunk_minutes: int | None
    max_chunk_minutes: int | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip() or not self.timezone_name:
            raise ValidationError("work routine identity/title/timezone are required")
        if self.dtstart_local.tzinfo is not None:
            raise ValidationError("work routine DTSTART must retain naive local civil semantics")
        if self.effort_minutes <= 0:
            raise ValidationError("work routine effort must be positive")
        if self.status not in {"ACTIVE", "CANCELLED"}:
            raise ValidationError("invalid work routine status")
        if self.version < 1:
            raise ValidationError("work routine version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class WorkRoutineOccurrence:
    account_id: str
    template_id: str
    original_recurrence_id: str
    task_id: str
    state: str
    override_title: str | None
    override_effort_minutes: int | None
    override_target_local: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not all((self.account_id, self.template_id, self.original_recurrence_id, self.task_id)):
            raise ValidationError("work routine occurrence identity is required")
        if self.state not in {"ACTIVE", "SKIPPED"}:
            raise ValidationError("invalid work routine occurrence state")
        if self.override_target_local is not None and self.override_target_local.tzinfo is not None:
            raise ValidationError("work routine target override must be naive local civil time")
        if self.override_effort_minutes is not None and self.override_effort_minutes <= 0:
            raise ValidationError("work routine effort override must be positive")
        if self.version < 1:
            raise ValidationError("work routine occurrence version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")

    @property
    def identity(self) -> tuple[str, str]:
        return self.template_id, self.original_recurrence_id
