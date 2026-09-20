from __future__ import annotations

from datetime import datetime, timedelta, timezone

from student_execution_os.connectors import SQLiteConnectorRepository
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    HardCutoff,
    Importance,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reconciliation import (
    ConflictProjection,
    ExtractionCertainty,
    ObservationValueType,
    SQLiteReconciliationRepository,
)
from student_execution_os.travel import LocationContextState, SQLiteTravelRepository, TravelEstimateSource

NOW = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
ACCOUNT = "ui-account"
OTHER_ACCOUNT = "other-account"


def seed_ui_database(path: str) -> None:
    with SQLiteCanonicalRepository(path, clock=FrozenClock(NOW)) as repo:
        repo.initialize()
        repo.create_account(ACCOUNT)
        repo.create_account(OTHER_ACCOUNT)

        repo.create_task(
            account_id=ACCOUNT,
            obligation_id="discrete",
            title="Discrete homework",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.HIGH,
            estimated_total_effort_minutes=90,
            estimated_total_effort_low_minutes=60,
            estimated_total_effort_high_minutes=120,
            remaining_effort_minutes=90,
            remaining_effort_low_minutes=60,
            remaining_effort_high_minutes=120,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=NOW,
            target_at=NOW + timedelta(hours=5),
            actual_cutoff=HardCutoff.known(NOW + timedelta(hours=9)),
            actor=ActorCategory.USER_UI,
        )
        repo.create_task(
            account_id=ACCOUNT,
            obligation_id="conflict-task",
            title="Compiler report",
            category=ObligationCategory.WORK,
            importance=Importance.CRITICAL,
            estimated_total_effort_minutes=45,
            remaining_effort_minutes=45,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(),
            actor=ActorCategory.USER_UI,
        )
        repo.create_task(
            account_id=ACCOUNT,
            obligation_id="override-task",
            title="Algorithms worksheet with a deliberately long title to exercise dense responsive rendering",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(),
            actor=ActorCategory.USER_UI,
        )
        repo.create_task(
            account_id=OTHER_ACCOUNT,
            obligation_id="other-secret",
            title="Other account secret task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.known(NOW + timedelta(days=1)),
            actor=ActorCategory.USER_UI,
        )

        travel = SQLiteTravelRepository(repo)
        travel.create_place(
            account_id=ACCOUNT,
            place_id="home",
            display_name="Home",
            alias="HOME",
            visibility_policy="PRIVATE_ALIAS",
            address="Secret exact address 123",
            latitude=55.75,
            longitude=37.61,
            actor=ActorCategory.SYSTEM,
        )
        travel.create_place(
            account_id=ACCOUNT,
            place_id="hse",
            display_name="HSE",
            alias="HSE",
            visibility_policy="PRIVATE_ALIAS",
            address="Another secret address",
            latitude=55.76,
            longitude=37.62,
            actor=ActorCategory.SYSTEM,
        )
        travel.set_current_location(
            account_id=ACCOUNT,
            state=LocationContextState.KNOWN,
            place_id="home",
            source="UI_FIXTURE",
            recorded_at=NOW,
            expires_at=NOW + timedelta(hours=12),
            actor=ActorCategory.SYSTEM,
        )
        travel.add_travel_estimate(
            account_id=ACCOUNT,
            estimate_id="home-hse",
            origin_place_id="home",
            destination_place_id="hse",
            transport_mode="TRANSIT",
            expected_duration_minutes=35,
            safe_duration_minutes=45,
            source=TravelEstimateSource.ROUTING_PROVIDER,
            source_revision="fixture-r1",
            calculated_at=NOW,
            expires_at=NOW + timedelta(hours=8),
            actor=ActorCategory.SYSTEM,
        )
        repo.create_fixed_event(
            account_id=ACCOUNT,
            obligation_id="lecture",
            title="Compilers lecture",
            starts_at=NOW + timedelta(hours=3),
            ends_at=NOW + timedelta(hours=4),
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(
                kind=LocationEffectKind.STAY,
                destination_place_id="hse",
            ),
            arrival_requirement_minutes=10,
            actor=ActorCategory.USER_UI,
        )
        repo.create_fixed_event(
            account_id=ACCOUNT,
            obligation_id="train",
            title="Booked train",
            starts_at=NOW + timedelta(hours=10),
            ends_at=NOW + timedelta(hours=12),
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(
                kind=LocationEffectKind.MOVE,
                origin_place_id="hse",
                destination_place_id="home",
            ),
            actor=ActorCategory.USER_UI,
        )

        recon = SQLiteReconciliationRepository(repo)
        for source_id, kind in (("google-calendar", "GOOGLE_CALENDAR"), ("teacher-chat", "TEACHER_MESSAGE")):
            recon.create_source_system(
                account_id=ACCOUNT,
                source_system_id=source_id,
                kind=kind,
                policy_context={"authority_group": "trusted"},
                actor=ActorCategory.SYSTEM,
            )
        recon.create_field_policy(
            account_id=ACCOUNT,
            field_path="actual_cutoff",
            version="ui-cutoff-v1",
            min_certainty=ExtractionCertainty.HIGH,
            source_authority={"context:trusted": 10},
            conflict_projection=ConflictProjection.EARLIEST_HARD_CUTOFF,
            actor=ActorCategory.SYSTEM,
        )
        for source_id, external_id, hours in (
            ("google-calendar", "calendar-conflict", 8),
            ("teacher-chat", "chat-conflict", 7),
        ):
            recon.bind_source_entity(
                account_id=ACCOUNT,
                source_system_id=source_id,
                external_entity_id=external_id,
                local_entity_id="conflict-task",
                match_decision_id=f"fixture:{source_id}",
                actor=ActorCategory.RECONCILER,
            )
            record = recon.add_source_record(
                account_id=ACCOUNT,
                source_system_id=source_id,
                external_entity_id=external_id,
                source_revision="r1",
                revision_order=1,
                actor=ActorCategory.CONNECTOR_INGESTION,
                metadata={"fixture": True},
            )
            recon.add_observation(
                account_id=ACCOUNT,
                source_record_id=record.id,
                field_path="actual_cutoff",
                value_type=ObservationValueType.HARD_CUTOFF,
                value=HardCutoff.known(NOW + timedelta(hours=hours)),
                extraction_certainty=ExtractionCertainty.EXACT,
                extractor_id=f"fixture:{source_id}",
                actor=ActorCategory.CONNECTOR_INGESTION,
            )

        recon.bind_source_entity(
            account_id=ACCOUNT,
            source_system_id="google-calendar",
            external_entity_id="calendar-override",
            local_entity_id="override-task",
            match_decision_id="fixture:override",
            actor=ActorCategory.RECONCILER,
        )
        override_record = recon.add_source_record(
            account_id=ACCOUNT,
            source_system_id="google-calendar",
            external_entity_id="calendar-override",
            source_revision="r1",
            revision_order=1,
            actor=ActorCategory.CONNECTOR_INGESTION,
            metadata={"fixture": True},
        )
        recon.add_observation(
            account_id=ACCOUNT,
            source_record_id=override_record.id,
            field_path="actual_cutoff",
            value_type=ObservationValueType.HARD_CUTOFF,
            value=HardCutoff.known(NOW + timedelta(hours=11)),
            extraction_certainty=ExtractionCertainty.EXACT,
            extractor_id="fixture:google-calendar",
            actor=ActorCategory.CONNECTOR_INGESTION,
        )
        recon.create_override(
            account_id=ACCOUNT,
            entity_ref="override-task",
            field_path="actual_cutoff",
            value_type=ObservationValueType.HARD_CUTOFF,
            value=HardCutoff.known(NOW + timedelta(hours=10)),
            actor=ActorCategory.USER_UI,
            reason="User confirmed an earlier hard cutoff",
            override_id="fixture-override",
        )

        connectors = SQLiteConnectorRepository(repo, recon)
        connectors.register(
            account_id=ACCOUNT,
            connector_id="google-calendar-primary",
            source_system_id="google-calendar",
            provider="google_calendar",
            scope="primary-hash",
            connector_version="fixture-v1",
        )
