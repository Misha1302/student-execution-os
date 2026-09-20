from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.reconciliation import Observation, SQLiteReconciliationRepository

from .model import (
    ExtractionObservationCandidate,
    ExtractionProposal,
    ToollessExtractionModel,
)


class ToollessExtractionContext:
    """Pure model-facing extraction boundary. It owns no mutation tools or repositories."""

    def run(
        self,
        *,
        source_record_id: str,
        extractor_id: str,
        content: str,
        model: ToollessExtractionModel,
    ) -> ExtractionProposal:
        if not source_record_id or not extractor_id:
            raise ValidationError("source_record_id and extractor_id are required")
        candidates = tuple(model.extract(content))
        if not all(
            isinstance(candidate, ExtractionObservationCandidate)
            for candidate in candidates
        ):
            raise ValidationError(
                "extraction model must return typed observation candidates only"
            )
        return ExtractionProposal(
            source_record_id=source_record_id,
            extractor_id=extractor_id,
            observations=candidates,
        )


class ExtractionIngestor:
    """Trusted server side that turns a typed proposal into immutable evidence."""

    def __init__(self, reconciliation: SQLiteReconciliationRepository) -> None:
        self.reconciliation = reconciliation

    def ingest(
        self,
        *,
        account_id: str,
        proposal: ExtractionProposal,
    ) -> tuple[Observation, ...]:
        self.reconciliation.get_source_record(
            account_id,
            proposal.source_record_id,
        )
        observations: list[Observation] = []
        for index, candidate in enumerate(proposal.observations):
            observation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    "|".join(
                        (
                            "student-execution-os:llm-extraction",
                            account_id,
                            proposal.source_record_id,
                            proposal.extractor_id,
                            str(index),
                            candidate.field_path,
                            candidate.value_type.value,
                            repr(candidate.value),
                        )
                    ),
                )
            )
            if self.reconciliation.observation_exists(account_id, observation_id):
                existing = self.reconciliation.get_observation(
                    account_id,
                    observation_id,
                )
                if (
                    existing.source_record_id != proposal.source_record_id
                    or existing.field_path != candidate.field_path
                    or existing.extractor_id != proposal.extractor_id
                ):
                    raise ValidationError(
                        "deterministic extraction observation collision"
                    )
                observations.append(existing)
                continue
            observations.append(
                self.reconciliation.add_observation(
                    account_id=account_id,
                    source_record_id=proposal.source_record_id,
                    binding_id=None,
                    field_path=candidate.field_path,
                    value_type=candidate.value_type,
                    value=candidate.value,
                    extraction_certainty=candidate.extraction_certainty,
                    extractor_id=proposal.extractor_id,
                    actor=ActorCategory.SYSTEM,
                    observation_id=observation_id,
                )
            )
        return tuple(observations)
