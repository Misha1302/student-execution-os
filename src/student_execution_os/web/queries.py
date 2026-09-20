from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from student_execution_os import __version__
from student_execution_os.agent import (
    ActionRequest,
    AgentCommand,
    AuthenticatedPrincipal,
    IntentStrength,
    SQLiteActionGateway,
)
from student_execution_os.connectors.model import ConnectorHealth
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    CutoffBoundary,
    CutoffState,
    EventTimeSemantics,
    HardCutoff,
    Importance,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
    TemporalPrecision,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.planning import (
    PlanningService,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.travel import SQLiteTravelRepository


def _jsonify(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {k: _jsonify(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonify(v) for v in value]
    return value


def _dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an offset")
    return parsed


def _cutoff(payload: dict[str, Any] | None) -> HardCutoff:
    payload = payload or {"state": "UNKNOWN"}
    state = CutoffState(payload.get("state", "UNKNOWN"))
    if state is CutoffState.ABSENT:
        return HardCutoff.absent()
    if state is CutoffState.UNKNOWN:
        precision = TemporalPrecision(payload.get("precision", TemporalPrecision.UNKNOWN.value))
        return HardCutoff.unknown(precision)
    at = _dt(payload.get("at"))
    if at is None:
        raise ValueError("KNOWN cutoff requires at")
    boundary = CutoffBoundary(payload.get("boundary", CutoffBoundary.INCLUSIVE.value))
    return HardCutoff.known(at, boundary)


class UiService:
    """Revision-bound UI/query/application façade.

    The client never supplies account or principal identity. The host binds those
    once when constructing the service; every request opens a fresh SQLite adapter
    and applies the bound account at the server boundary.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        account_id: str,
        principal_id: str,
        client_id: str = "web-ui",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = str(database)
        self.account_id = account_id
        self.principal = AuthenticatedPrincipal(
            account_id=account_id,
            principal_id=principal_id,
            client_id=client_id,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _repo(self) -> SQLiteCanonicalRepository:
        repo = SQLiteCanonicalRepository(self.database, clock=FrozenClock(self._now()))
        repo.initialize()
        repo._require_account(self.account_id)
        return repo

    def _snapshot(self, repo: SQLiteCanonicalRepository, *, hours: int = 36):
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("UiService clock must be timezone-aware")
        return build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=self.account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=hours),
            plan_output_horizon_end=now + timedelta(hours=min(hours, 36)),
        )

    @staticmethod
    def _task(task, *, risk=None, effective=None) -> dict[str, Any]:
        cutoff = task.actual_cutoff
        return {
            "id": task.obligation.id,
            "title": task.obligation.title,
            "description": task.obligation.description,
            "category": task.obligation.category.value,
            "importance": task.obligation.importance.value,
            "status": task.obligation.lifecycle_status.value,
            "version": task.obligation.version,
            "estimated_total_effort_minutes": task.estimated_total_effort_minutes,
            "estimated_total_effort_low_minutes": task.estimated_total_effort_low_minutes,
            "estimated_total_effort_high_minutes": task.estimated_total_effort_high_minutes,
            "remaining_effort_minutes": task.remaining_effort_minutes,
            "remaining_effort_low_minutes": task.remaining_effort_low_minutes,
            "remaining_effort_high_minutes": task.remaining_effort_high_minutes,
            "splittable": task.splittable,
            "min_chunk_minutes": task.min_chunk_minutes,
            "max_chunk_minutes": task.max_chunk_minutes,
            "actionable_from": _jsonify(task.actionable_from),
            "target_at": _jsonify(task.target_at),
            "actual_cutoff": {
                "state": cutoff.state.value,
                "at": _jsonify(cutoff.at),
                "boundary": cutoff.boundary.value if cutoff.boundary else None,
                "precision": cutoff.precision.value if cutoff.precision else None,
            },
            "risk": None if risk is None else {
                "state": risk.state.value,
                "basis": risk.basis.value,
                "reasons": list(risk.reasons),
                "latest_safe_start": _jsonify(risk.latest_safe_start),
                "policy_version": risk.policy_version,
            },
            "cutoff_truth": None if effective is None else {
                "state": effective.state.value,
                "evidence_ids": list(effective.evidence_ids),
                "policy_version": effective.policy_version,
                "reason": effective.reason,
                "conflict_id": effective.conflict_id,
                "override_id": effective.override_id,
                "planning_projection": _jsonify(effective.planning_projection),
                "admissible_cutoffs": _jsonify(effective.admissible_cutoffs),
            },
        }

    @staticmethod
    def _event(event) -> dict[str, Any]:
        return {
            "id": event.obligation.id,
            "title": event.obligation.title,
            "description": event.obligation.description,
            "category": event.obligation.category.value,
            "importance": event.obligation.importance.value,
            "status": event.obligation.lifecycle_status.value,
            "version": event.obligation.version,
            "starts_at": _jsonify(event.interval.starts_at),
            "ends_at": _jsonify(event.interval.ends_at),
            "time_semantics": event.time_semantics.value,
            "attendance_policy": event.attendance_policy.value,
            "location_effect": {
                "kind": event.location_effect.kind.value,
                "origin_place_id": event.location_effect.origin_place_id,
                "destination_place_id": event.location_effect.destination_place_id,
            },
            "arrival_requirement_minutes": event.arrival_requirement_minutes,
            "canonical": True,
        }

    def today(self) -> dict[str, Any]:
        with self._repo() as repo:
            snapshot = self._snapshot(repo, hours=36)
            store = SQLitePlanStore(repo)
            previous = store.get_latest(self.account_id)
            outcome = PlanningService().build(snapshot, now=self._now(), previous_plan=previous)
            store.save(outcome.plan)
            risks = {r.task_id: r for r in outcome.risks}
            reconciliation = SQLiteReconciliationRepository(repo)
            tasks = []
            for task in snapshot.tasks:
                tasks.append(self._task(
                    task,
                    risk=risks.get(task.obligation.id),
                    effective=reconciliation.get_effective_cutoff(self.account_id, task.obligation.id),
                ))
            events = [self._event(e) for e in snapshot.events]
            transitions = []
            travel_repo = SQLiteTravelRepository(repo)
            for t in snapshot.travel_projection.transitions:
                origin = travel_repo.get_place(self.account_id, t.origin_place_id)
                dest = travel_repo.get_place(self.account_id, t.destination_place_id)
                estimate_rows = repo.connection.execute(
                    "SELECT expected_duration_minutes,safe_duration_minutes,calculated_at,expires_at,source "
                    "FROM travel_estimates WHERE account_id=? AND id=?",
                    (self.account_id, t.travel_estimate_id),
                ).fetchone()
                transitions.append({
                    "origin": origin.alias or origin.display_name,
                    "destination": dest.alias or dest.display_name,
                    "target_event_id": t.target_event_id,
                    "travel_estimate_id": t.travel_estimate_id,
                    "travel_starts_at": _jsonify(t.travel_interval.starts_at),
                    "travel_ends_at": _jsonify(t.travel_interval.ends_at),
                    "latest_safe_departure": _jsonify(t.latest_safe_departure),
                    "arrival_buffer": _jsonify(t.arrival_buffer),
                    "arrival_requirement_minutes": t.arrival_requirement_minutes,
                    "expected_duration_minutes": None if estimate_rows is None else int(estimate_rows[0]),
                    "safe_duration_minutes": None if estimate_rows is None else int(estimate_rows[1]),
                    "calculated_at": None if estimate_rows is None else estimate_rows[2],
                    "expires_at": None if estimate_rows is None else estimate_rows[3],
                    "source": None if estimate_rows is None else estimate_rows[4],
                })
            return {
                "now": _jsonify(self._now()),
                "server_revision": snapshot.input_server_revision,
                "plan": self._plan_payload(outcome.plan, tasks, events, snapshot.constraints),
                "next_actions": _jsonify(outcome.next_actions),
                "tasks": tasks,
                "travel": {
                    "transitions": transitions,
                    "unknown_reasons": list(snapshot.travel_projection.unknown_reasons),
                    "infeasible_reasons": list(snapshot.travel_projection.infeasible_reasons),
                },
                "source_health": self._source_health(repo),
            }

    def _plan_payload(self, plan, tasks: list[dict[str, Any]], events: list[dict[str, Any]], constraints) -> dict[str, Any]:
        task_names = {t["id"]: t["title"] for t in tasks}
        event_names = {e["id"]: e["title"] for e in events}
        blocks = []
        for b in plan.blocks:
            label = b.type.value
            if b.obligation_id:
                label = task_names.get(b.obligation_id, label)
            elif b.source_event_id:
                label = event_names.get(b.source_event_id, label)
            blocks.append({
                "id": b.id,
                "type": b.type.value,
                "starts_at": _jsonify(b.starts_at),
                "ends_at": _jsonify(b.ends_at),
                "duration_minutes": b.duration_minutes,
                "label": label,
                "obligation_id": b.obligation_id,
                "source_event_id": b.source_event_id,
                "source_constraint_ids": list(b.source_constraint_ids),
                "travel_estimate_id": b.travel_estimate_id,
                "explanation": b.explanation,
                "ownership": "DERIVED",
            })
        return {
            "id": plan.id,
            "plan_revision": plan.plan_revision,
            "input_server_revision": plan.input_server_revision,
            "input_hash": plan.input_hash,
            "horizon_start": _jsonify(plan.horizon_start),
            "horizon_end": _jsonify(plan.horizon_end),
            "feasibility_status": plan.feasibility_status.value,
            "generated_at": _jsonify(plan.generated_at),
            "explanations": list(plan.explanations),
            "blocks": blocks,
            "canonical_events": events,
            "constraints": [{
                "id": c.id,
                "type": c.type.value,
                "starts_at": _jsonify(c.interval.starts_at),
                "ends_at": _jsonify(c.interval.ends_at),
                "obligation_id": c.obligation_id,
                "reason": c.reason,
                "version": c.version,
                "ownership": "CANONICAL",
            } for c in constraints],
        }

    def plan(self) -> dict[str, Any]:
        return self.today()["plan"]

    def tasks(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            source = SQLitePlanningStateSource(repo)
            reconciliation = SQLiteReconciliationRepository(repo)
            try:
                snapshot = self._snapshot(repo, hours=36)
                risks = {r.task_id: r for r in PlanningService().build(snapshot, now=self._now()).risks}
            except Exception:
                risks = {}
            return [
                self._task(
                    task,
                    risk=risks.get(task.obligation.id),
                    effective=reconciliation.get_effective_cutoff(self.account_id, task.obligation.id),
                )
                for task in source.list_tasks(self.account_id)
            ]

    def events(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return [self._event(e) for e in SQLitePlanningStateSource(repo).list_events(self.account_id)]

    def evidence(self) -> dict[str, Any]:
        with self._repo() as repo:
            recon = SQLiteReconciliationRepository(repo)
            source_rows = repo.connection.execute(
                "SELECT id,kind,policy_context_json,created_at FROM source_systems WHERE account_id=? ORDER BY id",
                (self.account_id,),
            ).fetchall()
            sources = []
            for row in source_rows:
                status = repo.connection.execute(
                    "SELECT status,recorded_at FROM source_status_history WHERE account_id=? AND source_system_id=? ORDER BY id DESC LIMIT 1",
                    (self.account_id, row["id"]),
                ).fetchone()
                connector = repo.connection.execute(
                    "SELECT id,provider,scope,health_status,last_successful_complete_sync_at,latest_failure_reason,connector_version,version "
                    "FROM connector_states WHERE account_id=? AND source_system_id=? ORDER BY id LIMIT 1",
                    (self.account_id, row["id"]),
                ).fetchone()
                sources.append({
                    "id": row["id"],
                    "kind": row["kind"],
                    "created_at": row["created_at"],
                    "availability": None if status is None else status["status"],
                    "availability_recorded_at": None if status is None else status["recorded_at"],
                    "connector": None if connector is None else dict(connector),
                })
            observations = repo.connection.execute(
                "SELECT o.id,o.binding_id,o.field_path,o.value_type,o.value_json,o.extraction_certainty,o.observed_at,o.extractor_id,"
                "sr.source_system_id,sr.external_entity_id,sr.source_revision "
                "FROM observations o JOIN source_records sr ON sr.id=o.source_record_id AND sr.account_id=o.account_id "
                "WHERE o.account_id=? ORDER BY o.observed_at DESC,o.id LIMIT 200",
                (self.account_id,),
            ).fetchall()
            conflicts = recon.list_conflicts(self.account_id)
            effective_rows = repo.connection.execute(
                "SELECT entity_ref FROM effective_fields WHERE account_id=? ORDER BY entity_ref",
                (self.account_id,),
            ).fetchall()
            effective = []
            for row in effective_rows:
                item = recon.get_effective_cutoff(self.account_id, row["entity_ref"])
                if item is not None:
                    effective.append(_jsonify(item))
            override_rows = repo.connection.execute(
                "SELECT id,entity_ref,field_path,value_type,value_json,status,reason,actor_category,created_at,version "
                "FROM user_overrides WHERE account_id=? ORDER BY created_at DESC,id",
                (self.account_id,),
            ).fetchall()
            return {
                "sources": sources,
                "observations": [dict(r) for r in observations],
                "conflicts": _jsonify(conflicts),
                "effective_fields": effective,
                "overrides": [dict(r) for r in override_rows],
            }

    def _source_health(self, repo: SQLiteCanonicalRepository) -> list[dict[str, Any]]:
        rows = repo.connection.execute(
            "SELECT id,provider,health_status,last_successful_complete_sync_at,latest_failure_reason,version "
            "FROM connector_states WHERE account_id=? ORDER BY id",
            (self.account_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def places(self) -> dict[str, Any]:
        with self._repo() as repo:
            travel = SQLiteTravelRepository(repo)
            current = travel.current_location(self.account_id)
            place_rows = repo.connection.execute(
                "SELECT id,alias,display_name,visibility_policy,version FROM places WHERE account_id=? ORDER BY display_name,id",
                (self.account_id,),
            ).fetchall()
            names = {r["id"]: (r["alias"] or r["display_name"]) for r in place_rows}
            estimate_rows = repo.connection.execute(
                "SELECT id,origin_place_id,destination_place_id,transport_mode,expected_duration_minutes,safe_duration_minutes,"
                "source,source_revision,calculated_at,expires_at FROM travel_estimates WHERE account_id=? ORDER BY calculated_at DESC,id",
                (self.account_id,),
            ).fetchall()
            now = self._now()
            estimates = []
            for row in estimate_rows:
                expires = _dt(row["expires_at"])
                estimates.append({
                    "id": row["id"],
                    "origin": names.get(row["origin_place_id"], row["origin_place_id"]),
                    "destination": names.get(row["destination_place_id"], row["destination_place_id"]),
                    "transport_mode": row["transport_mode"],
                    "expected_duration_minutes": row["expected_duration_minutes"],
                    "safe_duration_minutes": row["safe_duration_minutes"],
                    "source": row["source"],
                    "source_revision": row["source_revision"],
                    "calculated_at": row["calculated_at"],
                    "expires_at": row["expires_at"],
                    "fresh": expires is None or now < expires,
                })
            current_label = None
            if current.place_id is not None:
                current_label = names.get(current.place_id, current.place_id)
            return {
                "current_location": {
                    "state": current.effective_state_at(now).value,
                    "place": current_label,
                    "recorded_at": _jsonify(current.recorded_at),
                    "expires_at": _jsonify(current.expires_at),
                    "source": current.source,
                },
                "places": [dict(r) for r in place_rows],
                "route_estimates": estimates,
                "privacy": {
                    "exact_location_in_default_payload": False,
                    "note": "Exact address and coordinates remain server-side and are not serialized by this endpoint.",
                },
            }

    def diagnostics(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLitePlanStore(repo)
            latest = store.get_latest(self.account_id)
            return {
                "service": "student-execution-os",
                "version": __version__,
                "schema_version": repo.schema_version(),
                "server_revision": repo.get_server_revision(self.account_id),
                "account_binding": "server-bound",
                "principal_binding": "server-bound",
                "latest_plan": None if latest is None else {
                    "id": latest.id,
                    "plan_revision": latest.plan_revision,
                    "input_hash": latest.input_hash,
                    "feasibility_status": latest.feasibility_status.value,
                    "generated_at": _jsonify(latest.generated_at),
                },
                "connector_health": self._source_health(repo),
            }

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            task = repo.create_task(
                account_id=self.account_id,
                title=str(payload["title"]),
                description=payload.get("description"),
                category=ObligationCategory(payload.get("category", ObligationCategory.GENERAL.value)),
                importance=Importance(payload.get("importance", Importance.NORMAL.value)),
                estimated_total_effort_minutes=int(payload["estimated_total_effort_minutes"]),
                estimated_total_effort_low_minutes=payload.get("estimated_total_effort_low_minutes"),
                estimated_total_effort_high_minutes=payload.get("estimated_total_effort_high_minutes"),
                remaining_effort_minutes=int(payload.get("remaining_effort_minutes", payload["estimated_total_effort_minutes"])),
                remaining_effort_low_minutes=payload.get("remaining_effort_low_minutes"),
                remaining_effort_high_minutes=payload.get("remaining_effort_high_minutes"),
                splittable=bool(payload.get("splittable", True)),
                min_chunk_minutes=payload.get("min_chunk_minutes"),
                max_chunk_minutes=payload.get("max_chunk_minutes"),
                actionable_from=_dt(payload.get("actionable_from")),
                target_at=_dt(payload.get("target_at")),
                actual_cutoff=_cutoff(payload.get("actual_cutoff")),
                actor=ActorCategory.USER_UI,
            )
            return self._task(task)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            kwargs: dict[str, Any] = {}
            if "target_at" in payload:
                kwargs["target_at"] = _dt(payload.get("target_at"))
            if "actionable_from" in payload:
                kwargs["actionable_from"] = _dt(payload.get("actionable_from"))
            if "remaining_effort_minutes" in payload:
                kwargs["remaining_effort_minutes"] = int(payload["remaining_effort_minutes"])
            if "remaining_effort_low_minutes" in payload:
                kwargs["remaining_effort_low_minutes"] = payload.get("remaining_effort_low_minutes")
            if "remaining_effort_high_minutes" in payload:
                kwargs["remaining_effort_high_minutes"] = payload.get("remaining_effort_high_minutes")
            task = repo.update_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actor=ActorCategory.USER_UI,
                **kwargs,
            )
            return self._task(task)

    def lifecycle(self, obligation_id: str, action: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            method = {
                "complete": repo.complete_obligation,
                "cancel": repo.cancel_obligation,
                "reopen": repo.reopen_obligation,
            }.get(action)
            if method is None:
                raise ValueError("unsupported lifecycle action")
            ob = method(
                account_id=self.account_id,
                obligation_id=obligation_id,
                expected_version=expected_version,
                actor=ActorCategory.USER_UI,
            )
            return {
                "id": ob.id,
                "status": ob.lifecycle_status.value,
                "version": ob.version,
            }

    def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = LocationEffectKind(payload.get("location_effect", {}).get("kind", "NONE"))
        location = payload.get("location_effect") or {}
        effect = LocationEffect(
            kind=kind,
            origin_place_id=location.get("origin_place_id"),
            destination_place_id=location.get("destination_place_id"),
        )
        with self._repo() as repo:
            event = repo.create_event(
                account_id=self.account_id,
                title=str(payload["title"]),
                description=payload.get("description"),
                time_semantics=EventTimeSemantics.FIXED_INTERVAL,
                starts_at=_dt(payload.get("starts_at")),
                ends_at=_dt(payload.get("ends_at")),
                category=ObligationCategory(payload.get("category", ObligationCategory.GENERAL.value)),
                importance=Importance(payload.get("importance", Importance.NORMAL.value)),
                attendance_policy=AttendancePolicy(payload.get("attendance_policy", AttendancePolicy.REQUIRED.value)),
                location_effect=effect,
                arrival_requirement_minutes=int(payload.get("arrival_requirement_minutes", 0)),
                actor=ActorCategory.USER_UI,
            )
            return self._event(event)

    def update_event(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            current = repo.get_event(self.account_id, event_id)
            event = repo.update_fixed_event(
                account_id=self.account_id,
                obligation_id=event_id,
                expected_version=int(payload["expected_version"]),
                starts_at=_dt(payload.get("starts_at")) or current.interval.starts_at,
                ends_at=_dt(payload.get("ends_at")) or current.interval.ends_at,
                attendance_policy=(
                    AttendancePolicy(payload["attendance_policy"])
                    if payload.get("attendance_policy") else current.attendance_policy
                ),
                actor=ActorCategory.USER_UI,
            )
            return self._event(event)

    def agent_cancel_preview(self, obligation_id: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            gateway = SQLiteActionGateway(repo)
            ob = gateway.read_obligation(principal=self.principal, obligation_id=obligation_id)
            intent = gateway.mint_intent(
                principal=self.principal,
                command=AgentCommand.CANCEL_OBLIGATION,
                target_entity_id=obligation_id,
                intent_strength=IntentStrength.AMBIGUOUS,
                expected_version=expected_version,
            )
            return {
                "intent_id": intent.id,
                "command": intent.command.value,
                "target_entity_id": obligation_id,
                "target_title": ob.title,
                "expected_version": expected_version,
                "requires_confirmation": intent.requires_confirmation,
                "scope": "one obligation",
                "effect": "Lifecycle becomes CANCELLED; evidence history remains unchanged; current plan becomes stale.",
            }

    def agent_cancel_confirm_execute(self, intent_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        with self._repo() as repo:
            gateway = SQLiteActionGateway(repo)
            intent = gateway.confirm_intent(principal=self.principal, intent_id=intent_id)
            result = gateway.execute_cancel(
                principal=self.principal,
                request=ActionRequest(
                    intent_id=intent.id,
                    idempotency_key=idempotency_key or f"web:{intent.id}:{uuid4()}",
                    expected_version=intent.expected_version,
                ),
            )
            return _jsonify(result)
