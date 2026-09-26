from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Sequence

from student_execution_os.reconciliation import ExtractionCertainty, ObservationValueType


class IntentStrength(StrEnum):
    EXPLICIT_SCOPED = "EXPLICIT_SCOPED"
    AMBIGUOUS = "AMBIGUOUS"
    INFERRED = "INFERRED"


class AgentCommand(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    CREATE_EVENT = "CREATE_EVENT"
    REFINE_TASK = "REFINE_TASK"
    LOG_PROGRESS = "LOG_PROGRESS"
    COMPLETE_OBLIGATION = "COMPLETE_OBLIGATION"
    CANCEL_OBLIGATION = "CANCEL_OBLIGATION"
    CREATE_REMINDER = "CREATE_REMINDER"
    UPDATE_TASK = "UPDATE_TASK"
    UPDATE_EVENT = "UPDATE_EVENT"
    UPDATE_REMINDER = "UPDATE_REMINDER"
    RESCHEDULE = "RESCHEDULE"
    ARCHIVE_OBLIGATION = "ARCHIVE_OBLIGATION"
    SNOOZE = "SNOOZE"


@dataclass(frozen=True)
class ProposedAction:
    id: str
    command: AgentCommand
    payload: dict[str, Any]
    confidence: float
    unresolved_fields: tuple[str, ...]
    expected_version: int | None
    provenance: dict[str, str]
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ActionBatch:
    id: str
    actions: tuple[ProposedAction, ...]
    provider: str
    created_at: str
    expires_at: str


class ActionIntentStatus(StrEnum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    account_id: str
    principal_id: str
    client_id: str


@dataclass(frozen=True)
class ActionIntent:
    id: str
    account_id: str
    principal_id: str
    client_id: str
    command: AgentCommand
    target_entity_id: str
    intent_strength: IntentStrength
    expected_version: int
    requires_confirmation: bool
    confirmed_at: str | None
    status: ActionIntentStatus
    created_at: str
    consumed_at: str | None


@dataclass(frozen=True)
class ActionRequest:
    intent_id: str
    idempotency_key: str
    expected_version: int
    dry_run: bool = False


@dataclass(frozen=True)
class ActionResult:
    intent_id: str
    command: AgentCommand
    target_entity_id: str
    lifecycle_status: str
    entity_version: int
    replayed: bool
    dry_run: bool


@dataclass(frozen=True)
class ExtractionObservationCandidate:
    field_path: str
    value_type: ObservationValueType
    value: Any
    extraction_certainty: ExtractionCertainty


@dataclass(frozen=True)
class ExtractionProposal:
    source_record_id: str
    extractor_id: str
    observations: tuple[ExtractionObservationCandidate, ...]


class ToollessExtractionModel(Protocol):
    def extract(self, content: str) -> Sequence[ExtractionObservationCandidate]:
        ...


@dataclass(frozen=True)
class ExactLocationGrant:
    operation_id: str
    capability: str = "EXACT_LOCATION"


@dataclass(frozen=True)
class PrivatePlace:
    alias: str
    exact_address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    def llm_context(
        self,
        *,
        grant: ExactLocationGrant | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"alias": self.alias}
        if grant is not None:
            if not grant.operation_id or grant.capability != "EXACT_LOCATION":
                raise ValueError("invalid exact-location disclosure grant")
            payload["exact_address"] = self.exact_address
            if self.latitude is not None and self.longitude is not None:
                payload["coordinates"] = {
                    "latitude": self.latitude,
                    "longitude": self.longitude,
                }
        return payload
