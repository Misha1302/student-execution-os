from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from student_execution_os import __version__
from student_execution_os.agent import (
    ActionRequest,
    AgentCommand,
    AuthenticatedPrincipal,
    IntentStrength,
    SQLiteActionGateway,
)
from student_execution_os.connectors import (
    GoogleCalendarConnector,
    GoogleCalendarPage,
    SQLiteConnectorRepository,
)
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
from student_execution_os.notifications import NotificationKind, SQLiteNotificationRepository
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.planning import (
    FeasibilityEngine,
    PlanningService,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)
from student_execution_os.travel import (
    LocationContextState,
    SQLiteTravelRepository,
    TravelEstimateSource,
)

from student_execution_os.reconciliation import (
    ConflictProjection,
    EffectiveFieldState,
    ExtractionCertainty,
    ObservationValueType,
    SQLiteReconciliationRepository,
)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an offset")
    return parsed


def health_payload() -> dict[str, str]:
    return {
        "api_version": "0",
        "service": "student-execution-os",
        "status": "ok",
        "version": __version__,
    }


def run_domain_smoke(database: str) -> dict[str, object]:
    account_id = "smoke-account"
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        repo.create_account(account_id)
        task = repo.create_task(
            account_id=account_id,
            title="Pass 1 smoke task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=now,
            target_at=now + timedelta(hours=6),
            actual_cutoff=HardCutoff.known(now + timedelta(days=1)),
            actor=ActorCategory.SYSTEM,
        )
        task = repo.update_task(
            account_id=account_id,
            obligation_id=task.obligation.id,
            expected_version=task.obligation.version,
            remaining_effort_minutes=30,
            actor=ActorCategory.SYSTEM,
        )
        return {
            "account_id": account_id,
            "cutoff_state": task.actual_cutoff.state.value,
            "schema_version": repo.schema_version(),
            "server_revision": repo.get_server_revision(account_id),
            "status": "ok",
            "task_version": task.obligation.version,
        }


def run_feasibility_smoke() -> dict[str, object]:
    account_id = "feasibility-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(":memory:") as repo:
        repo.initialize()
        repo.create_account(account_id)
        repo.create_task(
            account_id=account_id,
            obligation_id="smoke-task",
            title="Pass 2 smoke task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=now,
            target_at=None,
            actual_cutoff=HardCutoff.known(now + timedelta(hours=2)),
            actor=ActorCategory.SYSTEM,
        )
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=2),
            plan_output_horizon_end=now + timedelta(hours=1),
        )
        result = FeasibilityEngine().evaluate(snapshot)
        return {
            "status": result.status.value,
            "input_hash": snapshot.input_hash,
            "server_revision": snapshot.input_server_revision,
            "witness_blocks": len(result.witness),
            "analysis_horizon_end": snapshot.analysis_horizon_end.isoformat(),
            "display_horizon_end": snapshot.plan_output_horizon_end.isoformat(),
        }


def run_planner_smoke() -> dict[str, object]:
    account_id = "planner-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(now)) as repo:
        repo.initialize()
        repo.create_account(account_id)
        repo.create_task(
            account_id=account_id,
            obligation_id="plan-task",
            title="Pass 3 smoke task",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.HIGH,
            estimated_total_effort_minutes=60,
            remaining_effort_minutes=60,
            remaining_effort_low_minutes=45,
            remaining_effort_high_minutes=75,
            splittable=True,
            min_chunk_minutes=30,
            max_chunk_minutes=60,
            actionable_from=now,
            target_at=now + timedelta(hours=2),
            actual_cutoff=HardCutoff.known(now + timedelta(hours=4)),
            actor=ActorCategory.SYSTEM,
        )
        repo.create_fixed_event(
            account_id=account_id,
            obligation_id="lecture",
            title="Lecture",
            starts_at=now + timedelta(hours=2),
            ends_at=now + timedelta(hours=3),
            attendance_policy=AttendancePolicy.REQUIRED,
            actor=ActorCategory.SYSTEM,
        )
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=4),
        )
        outcome = PlanningService().build(snapshot, now=now)
        store = SQLitePlanStore(repo)
        store.save(outcome.plan)
        return {
            "status": outcome.plan.feasibility_status.value,
            "plan_id": outcome.plan.id,
            "blocks": len(outcome.plan.blocks),
            "risks": {r.task_id: r.state.value for r in outcome.risks},
            "next_actions": len(outcome.next_actions),
            "persisted_current": store.get_current(account_id, snapshot.input_hash) is not None,
            "schema_version": repo.schema_version(),
        }


def run_reconciliation_smoke() -> dict[str, object]:
    account_id = "reconciliation-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(now)) as repo:
        repo.initialize()
        repo.create_account(account_id)
        repo.create_task(
            account_id=account_id,
            obligation_id="recon-task",
            title="Pass 4 smoke task",
            category=ObligationCategory.HOMEWORK,
            importance=Importance.HIGH,
            estimated_total_effort_minutes=30,
            remaining_effort_minutes=30,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(),
            actor=ActorCategory.SYSTEM,
        )
        reconciliation = SQLiteReconciliationRepository(repo)
        for source_id in ("lms", "chat"):
            reconciliation.create_source_system(
                account_id=account_id,
                source_system_id=source_id,
                kind="SMOKE_SOURCE",
                policy_context={"authority_group": "trusted"},
                actor=ActorCategory.SYSTEM,
            )
        reconciliation.create_field_policy(
            account_id=account_id,
            field_path="actual_cutoff",
            version="smoke-cutoff-v1",
            min_certainty=ExtractionCertainty.HIGH,
            source_authority={"context:trusted": 10},
            conflict_projection=ConflictProjection.EARLIEST_HARD_CUTOFF,
            actor=ActorCategory.SYSTEM,
        )
        for source_id, external_id, hours in (("lms", "e1", 2), ("chat", "e2", 4)):
            reconciliation.bind_source_entity(
                account_id=account_id,
                source_system_id=source_id,
                external_entity_id=external_id,
                local_entity_id="recon-task",
                match_decision_id=f"smoke:{source_id}",
                actor=ActorCategory.RECONCILER,
            )
            record = reconciliation.add_source_record(
                account_id=account_id,
                source_system_id=source_id,
                external_entity_id=external_id,
                source_revision="r1",
                revision_order=1,
                actor=ActorCategory.CONNECTOR_INGESTION,
            )
            reconciliation.add_observation(
                account_id=account_id,
                source_record_id=record.id,
                field_path="actual_cutoff",
                value_type=ObservationValueType.HARD_CUTOFF,
                value=HardCutoff.known(now + timedelta(hours=hours)),
                extraction_certainty=ExtractionCertainty.EXACT,
                extractor_id=f"smoke-extractor:{source_id}",
                actor=ActorCategory.CONNECTOR_INGESTION,
            )
        effective = reconciliation.get_effective_cutoff(account_id, "recon-task")
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=6),
        )
        context = snapshot.cutoff_reconciliation[0]
        if effective is None or effective.state is not EffectiveFieldState.CONFLICT:
            raise RuntimeError("reconciliation smoke did not preserve conflict truth")
        return {
            "status": "ok",
            "schema_version": repo.schema_version(),
            "effective_state": effective.state.value,
            "planning_cutoff": effective.planning_projection.at.isoformat() if effective.planning_projection else None,
            "conflict_visible": context.truth_state == EffectiveFieldState.CONFLICT.value,
            "server_revision": snapshot.input_server_revision,
        }


def run_connector_smoke() -> dict[str, object]:
    account_id = "connector-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

    class SmokeTransport:
        def list_events(self, *, calendar_id, sync_token, page_token):
            if calendar_id != "primary" or sync_token is not None or page_token is not None:
                raise RuntimeError("unexpected connector smoke request")
            return GoogleCalendarPage(
                items=(
                    {
                        "id": "smoke-event",
                        "updated": "2026-09-20T09:00:00Z",
                        "status": "confirmed",
                        "summary": "Connector smoke event",
                        "start": {"dateTime": "2026-09-21T09:00:00Z"},
                        "end": {"dateTime": "2026-09-21T10:00:00Z"},
                    },
                ),
                next_page_token=None,
                next_sync_token="smoke-sync-token",
            )

    with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(now)) as repo:
        repo.initialize()
        repo.create_account(account_id)
        reconciliation = SQLiteReconciliationRepository(repo)
        reconciliation.create_source_system(
            account_id=account_id,
            source_system_id="google-calendar",
            kind="GOOGLE_CALENDAR",
            policy_context={"provider": "google_calendar"},
            actor=ActorCategory.SYSTEM,
        )
        connectors = SQLiteConnectorRepository(repo, reconciliation)
        result = GoogleCalendarConnector(
            account_id=account_id,
            connector_id="google-calendar-primary",
            source_system_id="google-calendar",
            calendar_id="primary",
            transport=SmokeTransport(),
            reconciliation=reconciliation,
            connectors=connectors,
            sleeper=lambda _: None,
        ).sync()
        records = repo.connection.execute(
            "SELECT count(*) FROM source_records WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        return {
            "status": "ok",
            "schema_version": repo.schema_version(),
            "session_status": result.session.status.value,
            "connector_health": result.health.value,
            "checkpoint_present": result.checkpoint is not None,
            "source_records": records,
        }


def run_agent_smoke() -> dict[str, object]:
    account_id = "agent-smoke-account"
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    principal = AuthenticatedPrincipal(
        account_id=account_id,
        principal_id="smoke-user",
        client_id="smoke-client",
    )
    with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(now)) as repo:
        repo.initialize()
        repo.create_account(account_id)
        task = repo.create_task(
            account_id=account_id,
            obligation_id="agent-smoke-task",
            title="Agent smoke task",
            category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL,
            estimated_total_effort_minutes=10,
            remaining_effort_minutes=10,
            splittable=False,
            actual_cutoff=HardCutoff.unknown(),
            actor=ActorCategory.USER_UI,
        )
        gateway = SQLiteActionGateway(repo)
        intent = gateway.mint_intent(
            principal=principal,
            command=AgentCommand.CANCEL_OBLIGATION,
            target_entity_id=task.obligation.id,
            intent_strength=IntentStrength.EXPLICIT_SCOPED,
            expected_version=task.obligation.version,
            intent_id="agent-smoke-intent",
        )
        request = ActionRequest(
            intent_id=intent.id,
            idempotency_key="agent-smoke-key",
            expected_version=task.obligation.version,
        )
        first = gateway.execute_cancel(
            principal=principal,
            request=request,
        )
        replay = gateway.execute_cancel(
            principal=principal,
            request=request,
        )
        return {
            "status": "ok",
            "schema_version": repo.schema_version(),
            "lifecycle_status": first.lifecycle_status,
            "entity_version": first.entity_version,
            "replay": replay.replayed,
            "intent_status": gateway.get_intent(
                principal=principal,
                intent_id=intent.id,
            ).status.value,
        }


def run_travel_smoke() -> dict[str, object]:
    account_id = "travel-smoke-account"
    now = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
    with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(now)) as repo:
        repo.initialize()
        repo.create_account(account_id)
        travel = SQLiteTravelRepository(repo)
        for place_id in ("hse", "psychologist"):
            travel.create_place(
                account_id=account_id,
                place_id=place_id,
                display_name=place_id,
                alias=place_id.upper(),
                visibility_policy="PRIVATE_ALIAS",
                actor=ActorCategory.SYSTEM,
            )
        travel.set_current_location(
            account_id=account_id,
            state=LocationContextState.KNOWN,
            place_id="hse",
            source="SMOKE",
            actor=ActorCategory.SYSTEM,
            recorded_at=now,
            expires_at=now + timedelta(hours=12),
        )
        travel.add_travel_estimate(
            account_id=account_id,
            estimate_id="travel-smoke-route",
            origin_place_id="hse",
            destination_place_id="psychologist",
            transport_mode="TRANSIT",
            expected_duration_minutes=40,
            safe_duration_minutes=45,
            source=TravelEstimateSource.ROUTING_PROVIDER,
            source_revision="smoke",
            actor=ActorCategory.SYSTEM,
            calculated_at=now,
            expires_at=now + timedelta(hours=12),
        )
        repo.create_fixed_event(
            account_id=account_id,
            obligation_id="psychologist-event",
            title="Psychologist",
            starts_at=now.replace(hour=16),
            ends_at=now.replace(hour=17),
            attendance_policy=AttendancePolicy.REQUIRED,
            location_effect=LocationEffect(
                kind=LocationEffectKind.STAY,
                destination_place_id="psychologist",
            ),
            arrival_requirement_minutes=10,
            actor=ActorCategory.SYSTEM,
        )
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now.replace(hour=18),
            plan_output_horizon_end=now.replace(hour=18),
        )
        transition = snapshot.travel_projection.transitions[0]
        feasibility = FeasibilityEngine().evaluate(snapshot)
        return {
            "status": "ok",
            "schema_version": repo.schema_version(),
            "feasibility": feasibility.status.value,
            "latest_safe_departure": transition.latest_safe_departure.isoformat(),
            "arrival_buffer_minutes": transition.arrival_requirement_minutes,
            "travel_estimate_id": transition.travel_estimate_id,
        }


def run_recurrence_notification_smoke() -> dict[str, object]:
    account_id = "recurrence-notification-smoke-account"
    now = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)
    clock = FrozenClock(now)
    with SQLiteCanonicalRepository(":memory:", clock=clock) as repo:
        repo.initialize()
        repo.create_account(account_id)
        recurrence = SQLiteRecurrenceRepository(repo)
        template = recurrence.create_template(
            account_id=account_id,
            template_id="weekly-review",
            title="Weekly review",
            dtstart_local=datetime(2026, 10, 18, 9, 0),
            duration_minutes=30,
            recurrence_rule="FREQ=WEEKLY;COUNT=2",
            timezone_name="Europe/Amsterdam",
            actor=ActorCategory.SYSTEM,
        )
        original = "2026-10-25T09:00:00"
        recurrence.set_override(
            account_id=account_id, template_id=template.id,
            original_recurrence_id=original, action=OccurrenceOverrideAction.MODIFY,
            replacement_start_local=datetime(2026, 10, 25, 10, 0),
            actor=ActorCategory.SYSTEM,
        )
        occurrences = recurrence.expand(
            account_id=account_id, template_id=template.id,
            horizon_start=now, horizon_end=now + timedelta(days=9),
        )
        notifications = SQLiteNotificationRepository(repo)
        key = notifications.transition_suppression_key(
            kind=NotificationKind.SOURCE_CHANGE, entity_ref=template.id, transition_token="created",
        )
        item = notifications.schedule(
            account_id=account_id, suppression_key=key, kind=NotificationKind.SOURCE_CHANGE,
            scheduled_for=now + timedelta(minutes=10),
            domain_revision=repo.get_server_revision(account_id), entity_ref=template.id,
        )
        item = notifications.snooze(
            account_id=account_id, notification_id=item.id,
            until=now + timedelta(minutes=30), expected_version=item.version,
        )
        moved = next(o for o in occurrences if o.original_recurrence_id == original)
        return {
            "status": "ok",
            "schema_version": repo.schema_version(),
            "occurrence_identity": list(moved.identity),
            "moved_start": moved.starts_at.isoformat(),
            "notification_state": item.state.value,
            "notification_version": item.version,
            "server_revision": repo.get_server_revision(account_id),
        }


def _cutoff(value: str) -> HardCutoff:
    if value.upper() == "ABSENT":
        return HardCutoff.absent()
    if value.upper() == "UNKNOWN":
        return HardCutoff.unknown()
    return HardCutoff.known(_dt(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="student-execution-os",
        description="Student Execution OS command-line entrypoint.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health")
    subparsers.add_parser("version")

    domain_smoke = subparsers.add_parser("domain-smoke")
    domain_smoke.add_argument("--database", default=":memory:")
    subparsers.add_parser("feasibility-smoke")
    subparsers.add_parser("planner-smoke")
    subparsers.add_parser("reconciliation-smoke")
    subparsers.add_parser("connector-smoke")
    subparsers.add_parser("agent-smoke")
    subparsers.add_parser("travel-smoke")
    subparsers.add_parser("recurrence-notification-smoke")

    account = subparsers.add_parser("account-init")
    account.add_argument("--database", required=True)
    account.add_argument("--account", required=True)

    task = subparsers.add_parser("task-add")
    task.add_argument("--database", required=True)
    task.add_argument("--account", required=True)
    task.add_argument("--id")
    task.add_argument("--title", required=True)
    task.add_argument("--minutes", type=int, required=True)
    task.add_argument("--low-minutes", type=int)
    task.add_argument("--high-minutes", type=int)
    task.add_argument("--cutoff", required=True, help="ISO offset datetime, ABSENT, or UNKNOWN")
    task.add_argument("--target")
    task.add_argument("--actionable")
    task.add_argument("--importance", choices=[x.value for x in Importance], default=Importance.NORMAL.value)
    task.add_argument("--splittable", action="store_true")
    task.add_argument("--min-chunk", type=int)
    task.add_argument("--max-chunk", type=int)

    event = subparsers.add_parser("event-add")
    event.add_argument("--database", required=True)
    event.add_argument("--account", required=True)
    event.add_argument("--id")
    event.add_argument("--title", required=True)
    event.add_argument("--starts", required=True)
    event.add_argument("--ends", required=True)

    complete = subparsers.add_parser("task-complete")
    complete.add_argument("--database", required=True)
    complete.add_argument("--account", required=True)
    complete.add_argument("--id", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--database", required=True)
    plan.add_argument("--account", required=True)
    plan.add_argument("--now", required=True)
    plan.add_argument("--analysis-end", required=True)
    plan.add_argument("--display-end")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "health":
        print(json.dumps(health_payload(), sort_keys=True))
        return 0
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "domain-smoke":
        print(json.dumps(run_domain_smoke(args.database), sort_keys=True))
        return 0
    if args.command == "feasibility-smoke":
        print(json.dumps(run_feasibility_smoke(), sort_keys=True))
        return 0
    if args.command == "planner-smoke":
        print(json.dumps(run_planner_smoke(), sort_keys=True))
        return 0
    if args.command == "reconciliation-smoke":
        print(json.dumps(run_reconciliation_smoke(), sort_keys=True))
        return 0
    if args.command == "connector-smoke":
        print(json.dumps(run_connector_smoke(), sort_keys=True))
        return 0
    if args.command == "agent-smoke":
        print(json.dumps(run_agent_smoke(), sort_keys=True))
        return 0
    if args.command == "travel-smoke":
        print(json.dumps(run_travel_smoke(), sort_keys=True))
        return 0
    if args.command == "recurrence-notification-smoke":
        print(json.dumps(run_recurrence_notification_smoke(), sort_keys=True))
        return 0

    if args.command == "account-init":
        with SQLiteCanonicalRepository(args.database) as repo:
            repo.initialize()
            repo.create_account(args.account)
            print(json.dumps({"account_id": args.account, "status": "ok"}, sort_keys=True))
        return 0

    if args.command == "task-add":
        with SQLiteCanonicalRepository(args.database) as repo:
            repo.initialize()
            task = repo.create_task(
                account_id=args.account,
                obligation_id=args.id,
                title=args.title,
                category=ObligationCategory.GENERAL,
                importance=Importance(args.importance),
                estimated_total_effort_minutes=args.minutes,
                remaining_effort_minutes=args.minutes,
                remaining_effort_low_minutes=args.low_minutes,
                remaining_effort_high_minutes=args.high_minutes,
                splittable=args.splittable,
                min_chunk_minutes=args.min_chunk,
                max_chunk_minutes=args.max_chunk,
                actionable_from=_dt(args.actionable) if args.actionable else None,
                target_at=_dt(args.target) if args.target else None,
                actual_cutoff=_cutoff(args.cutoff),
                actor=ActorCategory.USER_UI,
            )
            print(json.dumps({"id": task.obligation.id, "version": task.obligation.version}, sort_keys=True))
        return 0

    if args.command == "event-add":
        with SQLiteCanonicalRepository(args.database) as repo:
            repo.initialize()
            event = repo.create_fixed_event(
                account_id=args.account,
                obligation_id=args.id,
                title=args.title,
                starts_at=_dt(args.starts),
                ends_at=_dt(args.ends),
                actor=ActorCategory.USER_UI,
            )
            print(json.dumps({"id": event.obligation.id, "version": event.obligation.version}, sort_keys=True))
        return 0

    if args.command == "task-complete":
        with SQLiteCanonicalRepository(args.database) as repo:
            repo.initialize()
            task = repo.get_task(args.account, args.id)
            obligation = repo.complete_obligation(
                account_id=args.account,
                obligation_id=args.id,
                expected_version=task.obligation.version,
                actor=ActorCategory.USER_UI,
            )
            print(json.dumps({"id": obligation.id, "status": obligation.lifecycle_status.value}, sort_keys=True))
        return 0

    if args.command == "plan":
        now = _dt(args.now)
        with SQLiteCanonicalRepository(args.database, clock=FrozenClock(now)) as repo:
            repo.initialize()
            source = SQLitePlanningStateSource(repo)
            snapshot = build_planning_snapshot(
                source,
                account_id=args.account,
                analysis_horizon_start=now,
                analysis_horizon_end=_dt(args.analysis_end),
                plan_output_horizon_end=_dt(args.display_end) if args.display_end else None,
            )
            store = SQLitePlanStore(repo)
            previous = store.get_latest(args.account)
            outcome = PlanningService().build(snapshot, now=now, previous_plan=previous)
            store.save(outcome.plan)
            print(json.dumps({
                "plan_id": outcome.plan.id,
                "plan_revision": outcome.plan.plan_revision,
                "input_hash": outcome.plan.input_hash,
                "feasibility": outcome.plan.feasibility_status.value,
                "blocks": [
                    {
                        "id": b.id,
                        "type": b.type.value,
                        "obligation_id": b.obligation_id,
                        "starts_at": b.starts_at.isoformat(),
                        "ends_at": b.ends_at.isoformat(),
                    }
                    for b in outcome.plan.blocks
                ],
                "risks": {r.task_id: r.state.value for r in outcome.risks},
                "next_actions": [
                    {
                        "task_id": a.task_id,
                        "what": a.what,
                        "duration_minutes": a.recommended_duration_minutes,
                        "why_now": a.why_now,
                    }
                    for a in outcome.next_actions
                ],
            }, sort_keys=True))
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")