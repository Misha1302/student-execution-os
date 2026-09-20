from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from student_execution_os.domain.model import HardCutoff, require_aware


class SourceAvailability(StrEnum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ExtractionCertainty(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXACT = "EXACT"

    @property
    def rank(self) -> int:
        return {
            ExtractionCertainty.UNKNOWN: 0,
            ExtractionCertainty.LOW: 1,
            ExtractionCertainty.MEDIUM: 2,
            ExtractionCertainty.HIGH: 3,
            ExtractionCertainty.EXACT: 4,
        }[self]


class ObservationValueType(StrEnum):
    HARD_CUTOFF = "HARD_CUTOFF"
    BOOLEAN = "BOOLEAN"
    STRING = "STRING"
    ABSENT = "ABSENT"
    SOURCE_REMOVED = "SOURCE_REMOVED"


class BindingState(StrEnum):
    ACTIVE = "ACTIVE"
    DETACHED = "DETACHED"
    SOURCE_REMOVED = "SOURCE_REMOVED"


class OverrideStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class EffectiveFieldState(StrEnum):
    RESOLVED = "RESOLVED"
    OVERRIDDEN = "OVERRIDDEN"
    ABSENT = "ABSENT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class ConflictStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"
    SUPERSEDED = "SUPERSEDED"


class ConflictProjection(StrEnum):
    NONE = "NONE"
    EARLIEST_HARD_CUTOFF = "EARLIEST_HARD_CUTOFF"


@dataclass(frozen=True)
class SourceSystem:
    id: str
    account_id: str
    kind: str
    policy_context: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.kind:
            raise ValueError("source system identity/account/kind are required")
        require_aware(self.created_at, "created_at")


@dataclass(frozen=True)
class SourceRecord:
    id: str
    account_id: str
    source_system_id: str
    external_entity_id: str | None
    source_revision: str | None
    revision_order: int | None
    observed_at: datetime
    content_hash: str | None
    source_uri: str | None
    raw_payload_ref: str | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.source_system_id:
            raise ValueError("source record identity/account/source are required")
        if self.source_revision is not None and self.revision_order is None:
            raise ValueError("source_revision requires an explicit comparable revision_order in Pass 4")
        if self.revision_order is not None and self.revision_order < 0:
            raise ValueError("revision_order cannot be negative")
        require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class Observation:
    id: str
    account_id: str
    source_record_id: str
    binding_id: str | None
    field_path: str
    value_type: ObservationValueType
    value: Any
    extraction_certainty: ExtractionCertainty
    observed_at: datetime
    extractor_id: str

    def __post_init__(self) -> None:
        if not all((self.id, self.account_id, self.source_record_id, self.field_path, self.extractor_id)):
            raise ValueError("observation identity/source/field/extractor are required")
        require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class SourceBinding:
    id: str
    account_id: str
    source_system_id: str
    external_entity_id: str
    local_entity_id: str
    state: BindingState
    match_decision_id: str
    version: int

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("source binding version must be >= 1")


@dataclass(frozen=True)
class FieldPolicy:
    account_id: str
    field_path: str
    version: str
    min_certainty: ExtractionCertainty
    source_authority: Mapping[str, int]
    freshness_rule: str
    allow_user_override: bool
    override_is_hard_planning_input: bool
    conflict_projection: ConflictProjection
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.account_id or not self.field_path or not self.version:
            raise ValueError("field policy identity is required")
        if self.freshness_rule != "REVISION_THEN_OBSERVED_AT":
            raise ValueError("unsupported freshness rule")
        require_aware(self.created_at, "created_at")

    def authority_for(self, source_system_id: str, policy_context: Mapping[str, Any]) -> int | None:
        if source_system_id in self.source_authority:
            return int(self.source_authority[source_system_id])
        group = policy_context.get("authority_group")
        if group is not None and f"context:{group}" in self.source_authority:
            return int(self.source_authority[f"context:{group}"])
        if "*" in self.source_authority:
            return int(self.source_authority["*"])
        return None


@dataclass(frozen=True)
class UserOverride:
    id: str
    account_id: str
    entity_ref: str
    field_path: str
    value_type: ObservationValueType
    value: Any
    status: OverrideStatus
    reason: str | None
    actor_category: str
    created_at: datetime
    version: int


@dataclass(frozen=True)
class EffectiveField:
    account_id: str
    entity_ref: str
    field_path: str
    state: EffectiveFieldState
    value_type: ObservationValueType | None
    value: Any
    evidence_ids: tuple[str, ...]
    policy_version: str
    planning_projection: HardCutoff | None
    admissible_cutoffs: tuple[HardCutoff, ...]
    reason: str | None
    conflict_id: str | None = None


def cutoff_evidence(
    cutoff: HardCutoff,
    *,
    raw_text: str | None = None,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    return {
        "state": cutoff.state.value,
        "at": cutoff.at.isoformat() if cutoff.at is not None else None,
        "boundary": cutoff.boundary.value if cutoff.boundary is not None else None,
        "precision": cutoff.precision.value if cutoff.precision is not None else None,
        "raw_text": raw_text,
        "timezone": timezone_name,
    }
