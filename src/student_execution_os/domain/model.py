from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Self

from student_execution_os.domain.errors import ValidationError


def require_aware(value: datetime | None, field: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValidationError(f"{field} must be offset-aware")


class ActorCategory(StrEnum):
    USER_UI = "USER_UI"
    USER_VIA_LLM = "USER_VIA_LLM"
    CONNECTOR_INGESTION = "CONNECTOR_INGESTION"
    RECONCILER = "RECONCILER"
    PLANNER = "PLANNER"
    SYSTEM = "SYSTEM"
    ADMIN = "ADMIN"


class ObligationKind(StrEnum):
    TASK = "TASK"
    EVENT = "EVENT"


class ObligationCategory(StrEnum):
    HOMEWORK = "HOMEWORK"
    LESSON = "LESSON"
    EXAM = "EXAM"
    MEETING = "MEETING"
    PERSONAL_APPOINTMENT = "PERSONAL_APPOINTMENT"
    WORK = "WORK"
    ADMIN = "ADMIN"
    ERRAND = "ERRAND"
    PERSONAL = "PERSONAL"
    GENERAL = "GENERAL"
    CUSTOM = "CUSTOM"


class LifecycleStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


class Importance(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class CutoffState(StrEnum):
    UNKNOWN = "UNKNOWN"
    ABSENT = "ABSENT"
    KNOWN = "KNOWN"


class CutoffBoundary(StrEnum):
    INCLUSIVE = "INCLUSIVE"  # completion <= t
    EXCLUSIVE = "EXCLUSIVE"  # completion < t


class TemporalPrecision(StrEnum):
    EXACT_INSTANT = "EXACT_INSTANT"
    DATE_ONLY = "DATE_ONLY"
    BOUNDED = "BOUNDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HardCutoff:
    state: CutoffState
    at: datetime | None = None
    boundary: CutoffBoundary | None = None
    precision: TemporalPrecision | None = None

    def __post_init__(self) -> None:
        require_aware(self.at, "actual_cutoff.at")
        if self.state is CutoffState.KNOWN:
            if self.at is None or self.boundary is None:
                raise ValidationError("KNOWN cutoff requires exact instant and boundary semantics")
            if self.precision is not TemporalPrecision.EXACT_INSTANT:
                raise ValidationError("KNOWN cutoff must have EXACT_INSTANT precision")
        elif self.at is not None or self.boundary is not None:
            raise ValidationError("ABSENT/UNKNOWN cutoff cannot carry an authoritative instant/boundary")

    @classmethod
    def known(cls, at: datetime, boundary: CutoffBoundary = CutoffBoundary.INCLUSIVE) -> Self:
        return cls(CutoffState.KNOWN, at, boundary, TemporalPrecision.EXACT_INSTANT)

    @classmethod
    def absent(cls) -> Self:
        return cls(CutoffState.ABSENT)

    @classmethod
    def unknown(cls, precision: TemporalPrecision = TemporalPrecision.UNKNOWN) -> Self:
        if precision is TemporalPrecision.EXACT_INSTANT:
            raise ValidationError("UNKNOWN cutoff cannot claim EXACT_INSTANT precision")
        return cls(CutoffState.UNKNOWN, precision=precision)


@dataclass(frozen=True)
class HalfOpenInterval:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if self.starts_at >= self.ends_at:
            raise ValidationError("half-open interval requires starts_at < ends_at")

    def overlaps(self, other: "HalfOpenInterval") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


@dataclass(frozen=True)
class Obligation:
    id: str
    account_id: str
    kind: ObligationKind
    category: ObligationCategory
    title: str
    description: str | None
    lifecycle_status: LifecycleStatus
    importance: Importance
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    version: int

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip():
            raise ValidationError("obligation id, account_id, and non-empty title are required")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        require_aware(self.completed_at, "completed_at")
        if self.version < 1:
            raise ValidationError("obligation version must be >= 1")

    def completed(self, now: datetime) -> Self:
        require_aware(now, "now")
        if self.lifecycle_status not in (LifecycleStatus.DRAFT, LifecycleStatus.ACTIVE):
            raise ValidationError("only draft/active obligations can be completed")
        return replace(
            self,
            lifecycle_status=LifecycleStatus.COMPLETED,
            completed_at=now,
            updated_at=now,
            version=self.version + 1,
        )

    def cancelled(self, now: datetime) -> Self:
        require_aware(now, "now")
        if self.lifecycle_status not in (LifecycleStatus.DRAFT, LifecycleStatus.ACTIVE):
            raise ValidationError("only draft/active obligations can be cancelled")
        return replace(
            self,
            lifecycle_status=LifecycleStatus.CANCELLED,
            completed_at=None,
            updated_at=now,
            version=self.version + 1,
        )

    def reopened(self, now: datetime) -> Self:
        require_aware(now, "now")
        if self.lifecycle_status not in (LifecycleStatus.COMPLETED, LifecycleStatus.CANCELLED):
            raise ValidationError("only completed/cancelled obligations can be reopened")
        return replace(
            self,
            lifecycle_status=LifecycleStatus.ACTIVE,
            completed_at=None,
            updated_at=now,
            version=self.version + 1,
        )


@dataclass(frozen=True)
class Task:
    obligation: Obligation
    estimated_total_effort_minutes: int
    remaining_effort_minutes: int
    splittable: bool
    min_chunk_minutes: int | None
    max_chunk_minutes: int | None
    actionable_from: datetime | None
    actual_cutoff: HardCutoff
    target_at: datetime | None

    def __post_init__(self) -> None:
        if self.obligation.kind is not ObligationKind.TASK:
            raise ValidationError("Task requires TASK obligation kind")
        if self.estimated_total_effort_minutes <= 0:
            raise ValidationError("estimated total effort must be positive")
        if self.remaining_effort_minutes < 0:
            raise ValidationError("remaining effort cannot be negative")
        if self.min_chunk_minutes is not None and self.min_chunk_minutes <= 0:
            raise ValidationError("min_chunk_minutes must be positive")
        if self.max_chunk_minutes is not None and self.max_chunk_minutes <= 0:
            raise ValidationError("max_chunk_minutes must be positive")
        if (
            self.min_chunk_minutes is not None
            and self.max_chunk_minutes is not None
            and self.max_chunk_minutes < self.min_chunk_minutes
        ):
            raise ValidationError("max_chunk_minutes must be >= min_chunk_minutes")
        if not self.splittable and self.remaining_effort_minutes > 0:
            if self.min_chunk_minutes is not None and self.remaining_effort_minutes < self.min_chunk_minutes:
                raise ValidationError("non-splittable remaining effort is shorter than configured minimum block")
            if self.max_chunk_minutes is not None and self.remaining_effort_minutes > self.max_chunk_minutes:
                raise ValidationError("non-splittable remaining effort exceeds configured maximum block")
        require_aware(self.actionable_from, "actionable_from")
        require_aware(self.target_at, "target_at")


class EventTimeSemantics(StrEnum):
    FIXED_INTERVAL = "FIXED_INTERVAL"
    FLEXIBLE_WINDOW = "FLEXIBLE_WINDOW"


class AttendancePolicy(StrEnum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    PREFERRED = "PREFERRED"


class LocationEffectKind(StrEnum):
    NONE = "NONE"
    REMOTE = "REMOTE"
    STAY = "STAY"
    MOVE = "MOVE"


@dataclass(frozen=True)
class LocationEffect:
    kind: LocationEffectKind = LocationEffectKind.NONE
    origin_place_id: str | None = None
    destination_place_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is LocationEffectKind.STAY:
            if not self.destination_place_id or self.origin_place_id is not None:
                raise ValidationError("STAY requires destination_place_id only")
        elif self.kind is LocationEffectKind.MOVE:
            if not self.origin_place_id or not self.destination_place_id:
                raise ValidationError("MOVE requires origin and destination place ids")
        elif self.origin_place_id is not None or self.destination_place_id is not None:
            raise ValidationError("NONE/REMOTE location effects cannot carry place ids")


@dataclass(frozen=True)
class Event:
    obligation: Obligation
    time_semantics: EventTimeSemantics
    interval: HalfOpenInterval
    attendance_policy: AttendancePolicy
    location_effect: LocationEffect

    def __post_init__(self) -> None:
        if self.obligation.kind is not ObligationKind.EVENT:
            raise ValidationError("Event requires EVENT obligation kind")
        if self.time_semantics is not EventTimeSemantics.FIXED_INTERVAL:
            raise ValidationError("Event model currently materializes FIXED_INTERVAL only")


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class Project:
    id: str
    account_id: str
    title: str
    description: str | None
    status: ProjectStatus
    importance: Importance | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip():
            raise ValidationError("project id, account_id, and non-empty title are required")
        if self.version < 1:
            raise ValidationError("project version must be >= 1")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


class MilestoneOwnerKind(StrEnum):
    OBLIGATION = "OBLIGATION"
    PROJECT = "PROJECT"


class MilestoneRole(StrEnum):
    INTERMEDIATE = "INTERMEDIATE"
    PENALTY_START = "PENALTY_START"
    FINAL_CUTOFF = "FINAL_CUTOFF"


class MilestoneStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Milestone:
    id: str
    account_id: str
    owner_kind: MilestoneOwnerKind
    owner_id: str
    title: str
    marker_at: datetime
    role: MilestoneRole
    consequence: str | None
    hard_for_planning: bool
    status: MilestoneStatus
    version: int

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.owner_id or not self.title.strip():
            raise ValidationError("milestone identity, owner, account, and title are required")
        require_aware(self.marker_at, "marker_at")
        if self.version < 1:
            raise ValidationError("milestone version must be >= 1")


class DependencySuccessorKind(StrEnum):
    TASK = "TASK"
    EVENT = "EVENT"
    MILESTONE = "MILESTONE"


@dataclass(frozen=True)
class Dependency:
    id: str
    account_id: str
    predecessor_task_id: str
    successor_kind: DependencySuccessorKind
    successor_id: str

    def __post_init__(self) -> None:
        if not all((self.id, self.account_id, self.predecessor_task_id, self.successor_id)):
            raise ValidationError("dependency identity/account/predecessor/successor are required")


class UserTimeConstraintType(StrEnum):
    FIXED_PERSONAL_BLOCK = "FIXED_PERSONAL_BLOCK"
    UNAVAILABLE = "UNAVAILABLE"
    PINNED_WORK = "PINNED_WORK"


@dataclass(frozen=True)
class UserTimeConstraint:
    id: str
    account_id: str
    type: UserTimeConstraintType
    interval: HalfOpenInterval
    obligation_id: str | None
    reason: str | None
    version: int

    def __post_init__(self) -> None:
        if not self.id or not self.account_id:
            raise ValidationError("constraint id and account_id are required")
        if self.type is UserTimeConstraintType.PINNED_WORK and not self.obligation_id:
            raise ValidationError("PINNED_WORK requires obligation_id")
        if self.version < 1:
            raise ValidationError("constraint version must be >= 1")
