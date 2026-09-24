from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from student_execution_os import __version__
from student_execution_os.agent import (
    ActionRequest,
    AgentCommand,
    AuthenticatedPrincipal,
    IntentStrength,
    SQLiteActionGateway,
    SQLiteAssistantService,
    assistant_capabilities,
    assistant_provider_from_environment,
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
    EventLocationOption,
    HardCutoff,
    Importance,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
    TemporalPrecision,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.persistence.product import SQLiteAttachmentRepository, SQLiteSavedViewRepository
from student_execution_os.persistence.metrics import SQLiteOperationalMetrics
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.reminders import ReminderStore, provider_from_environment
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.planning import (
    PlanningService,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)
from student_execution_os.planning.model import PlanningPolicy
from student_execution_os.planning.outlook import (
    SQLitePlanningProfileRepository,
    overlap_minutes,
    planning_intervals,
)
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.travel import SQLiteTravelRepository
from student_execution_os.sync.commands import SyncService


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


def _local_dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("local civil datetime must not include an offset")
    return parsed


def _local_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


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
    when constructing the service (once for a bound server, per request from the
    authenticated session otherwise); every request opens a fresh SQLite adapter
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
        binding: str = "server-bound",
    ) -> None:
        self.database = str(database)
        self.account_id = account_id
        self.binding = binding
        self.principal = AuthenticatedPrincipal(
            account_id=account_id,
            principal_id=principal_id,
            client_id=client_id,
        )
        # The planner works on whole minutes; a wall clock with seconds would make every
        # plan UNKNOWN (UNSUPPORTED_SUB_MINUTE_TIME).
        self._now = now or (lambda: datetime.now(timezone.utc).replace(second=0, microsecond=0))

    def _repo(self) -> SQLiteCanonicalRepository:
        repo = SQLiteCanonicalRepository(self.database, clock=FrozenClock(self._now()))
        repo.initialize()
        repo._require_account(self.account_id)
        return repo

    def _snapshot(self, repo: SQLiteCanonicalRepository, *, hours: int = 36):
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("UiService clock must be timezone-aware")
        profile = SQLitePlanningProfileRepository(repo).get(self.account_id)
        return build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=self.account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=hours),
            plan_output_horizon_end=now + timedelta(hours=min(hours, 36)),
            policy=PlanningPolicy(
                version=f"daily-product-v1:{profile.version}:{profile.optional_event_policy}",
                optional_event_policy=profile.optional_event_policy,
            ),
        )

    def planning_profile(self) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLitePlanningProfileRepository(repo).get(self.account_id).payload()

    def update_planning_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLitePlanningProfileRepository(repo).update(self.account_id, payload).payload()

    @staticmethod
    def _merged_minutes(intervals: list[tuple[datetime, datetime]]) -> int:
        if not intervals:
            return 0
        ordered = sorted(intervals)
        merged: list[list[datetime]] = []
        for start, end in ordered:
            if not merged or start >= merged[-1][1]:
                merged.append([start, end])
            elif end > merged[-1][1]:
                merged[-1][1] = end
        return sum(int((end - start).total_seconds() // 60) for start, end in merged)

    def outlook(self, range_name: str, anchor: str | None) -> dict[str, Any]:
        if range_name not in {"week", "month"}:
            raise ValueError("outlook range must be week or month")
        with self._repo() as repo:
            profile = SQLitePlanningProfileRepository(repo).get(self.account_id)
            from zoneinfo import ZoneInfo
            local_now = self._now().astimezone(ZoneInfo(profile.timezone_name))
            anchor_date = datetime.fromisoformat(anchor).date() if anchor else local_now.date()
            if range_name == "week":
                delta = (anchor_date.isoweekday() - profile.first_day_of_week) % 7
                start_date = anchor_date - timedelta(days=delta)
                days = 7
            else:
                month_start = anchor_date.replace(day=1)
                delta = (month_start.isoweekday() - profile.first_day_of_week) % 7
                start_date = month_start - timedelta(days=delta)
                days = 42
            zone = ZoneInfo(profile.timezone_name)
            horizon_start = datetime.combine(start_date, datetime.min.time(), zone).astimezone(timezone.utc)
            horizon_end = datetime.combine(start_date + timedelta(days=days), datetime.min.time(), zone).astimezone(timezone.utc)
            snapshot = build_planning_snapshot(
                SQLitePlanningStateSource(repo), account_id=self.account_id,
                analysis_horizon_start=horizon_start, analysis_horizon_end=horizon_end,
                plan_output_horizon_start=horizon_start, plan_output_horizon_end=horizon_end,
                policy=PlanningPolicy(
                    version=f"outlook-v1:{profile.version}:{profile.optional_event_policy}",
                    optional_event_policy=profile.optional_event_policy,
                ),
            )
            outcome = PlanningService().build(snapshot, now=self._now())
            windows = planning_intervals(profile, start_date, days)
            blocks = list(outcome.plan.blocks)
            constraints = list(snapshot.constraints)
            risks = {risk.task_id: risk for risk in outcome.risks}
            day_rows = []
            for index in range(days):
                day = start_date + timedelta(days=index)
                day_windows = [window for window in windows if window[0].astimezone(zone).date() == day]
                capacity = sum(int((end - start).total_seconds() // 60) for start, end in day_windows)
                occupied_parts: list[tuple[datetime, datetime]] = []
                work_parts: list[tuple[datetime, datetime]] = []
                occupancy = {"WORK": 0, "EVENT": 0, "TRAVEL": 0, "BUFFER": 0, "CONSTRAINT": 0}
                for block in blocks:
                    interval = (block.starts_at, block.ends_at)
                    clipped = [(max(interval[0], w[0]), min(interval[1], w[1])) for w in day_windows if overlap_minutes(interval, w)]
                    minutes = sum(int((end - start).total_seconds() // 60) for start, end in clipped)
                    if not minutes:
                        continue
                    key = {"WORK": "WORK", "EVENT_PROJECTION": "EVENT", "TRAVEL_TRANSITION": "TRAVEL", "BUFFER": "BUFFER"}[block.type.value]
                    occupancy[key] += minutes
                    occupied_parts.extend(clipped)
                    if key == "WORK":
                        work_parts.extend(clipped)
                for constraint in constraints:
                    interval = (constraint.interval.starts_at, constraint.interval.ends_at)
                    clipped = [(max(interval[0], w[0]), min(interval[1], w[1])) for w in day_windows if overlap_minutes(interval, w)]
                    occupancy["CONSTRAINT"] += sum(int((end - start).total_seconds() // 60) for start, end in clipped)
                    occupied_parts.extend(clipped)
                deadlines = [
                    {"task_id": task.obligation.id, "title": task.obligation.title, "at": _jsonify(task.actual_cutoff.at)}
                    for task in snapshot.tasks
                    if task.actual_cutoff.at is not None and task.actual_cutoff.at.astimezone(zone).date() == day
                ]
                day_rows.append({
                    "date": day.isoformat(), "planning_capacity_minutes": capacity,
                    "occupancy": occupancy, "planned_load_minutes": self._merged_minutes(work_parts),
                    "free_capacity_minutes": max(0, capacity - self._merged_minutes(occupied_parts)),
                    "shortfall_minutes": max(0, self._merged_minutes(occupied_parts) - capacity),
                    "deadlines": deadlines,
                    "risk": [
                        {"task_id": task_id, "state": risk.state.value, "latest_safe_start": _jsonify(risk.latest_safe_start)}
                        for task_id, risk in risks.items()
                        if risk.latest_safe_start is not None and risk.latest_safe_start.astimezone(zone).date() == day
                    ],
                    "provisional": outcome.plan.feasibility_status.value == "UNKNOWN",
                })
            weeks = []
            if range_name == "month":
                for offset in range(0, 42, 7):
                    chunk = day_rows[offset:offset + 7]
                    weeks.append({
                        "starts_on": chunk[0]["date"],
                        "planning_capacity_minutes": sum(row["planning_capacity_minutes"] for row in chunk),
                        "planned_load_minutes": sum(row["planned_load_minutes"] for row in chunk),
                        "free_capacity_minutes": sum(row["free_capacity_minutes"] for row in chunk),
                        "risk_days": [row["date"] for row in chunk if row["risk"] or row["shortfall_minutes"]],
                    })
            return {
                "range": range_name, "anchor": anchor_date.isoformat(),
                "horizon_start": _jsonify(horizon_start), "horizon_end": _jsonify(horizon_end),
                "analysis_horizon_end": _jsonify(snapshot.analysis_horizon_end),
                "profile": profile.payload(), "feasibility_status": outcome.plan.feasibility_status.value,
                "uncertainty_reasons": list(outcome.plan.explanations) if outcome.plan.feasibility_status.value == "UNKNOWN" else [],
                "days": day_rows, "weeks": weeks,
            }

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
            "started_at": _jsonify(task.started_at),
            "last_progress_at": _jsonify(task.last_progress_at),
            "completed_at": _jsonify(task.obligation.completed_at),
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
            "location_options": [{
                "id": option.id, "label": option.label,
                "location_effect": {"kind": option.effect.kind.value,
                                    "origin_place_id": option.effect.origin_place_id,
                                    "destination_place_id": option.effect.destination_place_id},
            } for option in event.location_options],
            "selected_location_option_id": event.selected_location_option_id,
            "arrival_requirement_minutes": event.arrival_requirement_minutes,
            "canonical": True,
        }

    def today(self) -> dict[str, Any]:
        with self._repo() as repo:
            planning_started = perf_counter()
            snapshot = self._snapshot(repo, hours=36)
            store = SQLitePlanStore(repo)
            previous = store.get_latest(self.account_id)
            outcome = PlanningService().build(snapshot, now=self._now(), previous_plan=previous)
            store.save(outcome.plan)
            metrics = SQLiteOperationalMetrics(repo)
            metrics.record(
                "plan_run_latency_ms",
                (perf_counter() - planning_started) * 1000,
                account_id=self.account_id,
                correlation_id=outcome.plan.id,
                dimensions={"status": outcome.plan.feasibility_status.value, "surface": "today"},
            )
            budget_exhausted = sum(
                1 for risk in outcome.risks if "RISK_EVALUATION_BUDGET_EXHAUSTED" in risk.reasons
            )
            metrics.record(
                "solver_budget_exhausted_count",
                budget_exhausted,
                account_id=self.account_id,
                correlation_id=outcome.plan.id,
            )
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
            all_tasks = [self._task(task) for task in SQLitePlanningStateSource(repo).list_tasks(self.account_id)]
            needs_refinement = [task for task in all_tasks if task["status"] == "DRAFT"]
            active_tasks = {task["id"]: task for task in tasks}
            now = self._now()
            current_block = next((
                block for block in outcome.plan.blocks
                if block.type.value == "WORK" and block.starts_at <= now < block.ends_at
            ), None)
            current_action = None
            if current_block is not None:
                task = active_tasks.get(current_block.obligation_id)
                current_action = {
                    "task_id": current_block.obligation_id,
                    "title": None if task is None else task["title"],
                    "starts_at": _jsonify(current_block.starts_at),
                    "ends_at": _jsonify(current_block.ends_at),
                    "source": "CURRENT_WORK_BLOCK",
                }
            elif outcome.next_actions:
                first = outcome.next_actions[0]
                current_action = {
                    "task_id": first.task_id,
                    "title": first.what,
                    "recommended_duration_minutes": first.recommended_duration_minutes,
                    "relevant_at": _jsonify(first.relevant_at),
                    "source": "NEXT_ACTION",
                }
            interruptions = [
                block for block in self._plan_payload(outcome.plan, tasks, events, snapshot.constraints)["blocks"]
                if block["type"] in {"EVENT_PROJECTION", "TRAVEL_TRANSITION", "BUFFER"}
                and _dt(block["ends_at"]) >= now
            ][:5]
            repair_actions = [] if outcome.plan.feasibility_status.value == "FEASIBLE" else [{
                "kind": "REPAIR_PLAN",
                "status": outcome.plan.feasibility_status.value,
                "reasons": list(outcome.plan.explanations),
            }]
            plan_payload = self._plan_payload(outcome.plan, tasks, events, snapshot.constraints)
            return {
                "now": _jsonify(self._now()),
                "server_revision": snapshot.input_server_revision,
                "plan": plan_payload,
                "current_action": current_action,
                "interruptions": interruptions,
                "repair_actions": repair_actions,
                "needs_refinement": needs_refinement,
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
            label = (
                task_names.get(b.obligation_id)
                or event_names.get(b.obligation_id)
                or event_names.get(b.source_event_id)
                or b.type.value
            )
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

    @staticmethod
    def _recurring_template_payload(template) -> dict[str, Any]:
        return {
            "id": template.id,
            "title": template.title,
            "description": template.description,
            "category": template.category.value,
            "importance": template.importance.value,
            "dtstart_local": _local_iso(template.dtstart_local),
            "duration_minutes": template.duration_minutes,
            "recurrence_rule": template.recurrence_rule.canonical(),
            "timezone_name": template.timezone_name,
            "attendance_policy": template.attendance_policy.value,
            "location_effect": {
                "kind": template.location_effect.kind.value,
                "origin_place_id": template.location_effect.origin_place_id,
                "destination_place_id": template.location_effect.destination_place_id,
            },
            "arrival_requirement_minutes": template.arrival_requirement_minutes,
            "resolution_policy": template.resolution_policy.value,
            "series_end_before_local": _local_iso(template.series_end_before_local),
            "version": template.version,
            "ownership": "CANONICAL_RULE",
        }

    def calendar(self) -> dict[str, Any]:
        with self._repo() as repo:
            source = SQLitePlanningStateSource(repo)
            recurrence = SQLiteRecurrenceRepository(repo)
            start = self._now() - timedelta(days=1)
            end = self._now() + timedelta(days=30)
            templates = recurrence.list_templates(self.account_id)
            occurrences: list[dict[str, Any]] = []
            for template in templates:
                for item in recurrence.expand(
                    account_id=self.account_id, template_id=template.id,
                    horizon_start=start, horizon_end=end,
                ):
                    occurrences.append({
                        "template_id": item.template_id,
                        "original_recurrence_id": item.original_recurrence_id,
                        "starts_at": _jsonify(item.starts_at),
                        "ends_at": _jsonify(item.ends_at),
                        "cancelled": item.cancelled,
                        "override_id": item.override_id,
                        "identity": [item.template_id, item.original_recurrence_id],
                        "ownership": "DERIVED_OCCURRENCE",
                    })
            occurrences.sort(key=lambda item: (item["starts_at"], item["template_id"], item["original_recurrence_id"]))
            return {
                "events": [self._event(e) for e in source.list_events(self.account_id)],
                "recurring_templates": [self._recurring_template_payload(t) for t in templates],
                "occurrences": occurrences,
                "horizon_start": _jsonify(start),
                "horizon_end": _jsonify(end),
            }

    def notifications(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return ReminderStore(repo).messages(self.account_id, since=self._now() - timedelta(days=30))

    def notification_preferences(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = ReminderStore(repo)
            return store.prefs_payload(store.prefs(self.account_id))

    def update_notification_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            store = ReminderStore(repo)
            return store.prefs_payload(store.update_prefs(self.account_id, payload))

    def devices(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return ReminderStore(repo).devices(self.account_id)

    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return ReminderStore(repo).register_device(
                self.account_id, str(payload.get("token", "")), payload.get("label")
            )

    def revoke_device(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            expected = int(payload["expected_version"])
            store = ReminderStore(repo)
            if int(store.device(self.account_id, device_id)["version"]) != expected:
                from student_execution_os.domain.errors import VersionConflict
                raise VersionConflict("device registration version changed")
            return store.revoke_device(self.account_id, device_id)

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

    def account_export(self) -> dict[str, Any]:
        return SQLiteDataLifecycle(self.database, now=self._now).export_account(self.account_id).to_dict()

    def account_deletion_policy(self) -> dict[str, Any]:
        with self._repo() as repo:
            revision = repo.get_server_revision(self.account_id)
        policy = SQLiteDataLifecycle(self.database, now=self._now).deletion_policy()
        return {
            "account_id": self.account_id,
            "server_revision": revision,
            **policy,
        }

    def delete_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = SQLiteDataLifecycle(self.database, now=self._now).delete_account(
            self.account_id,
            expected_server_revision=int(payload["expected_server_revision"]),
            confirm_account_id=str(payload.get("confirm_account_id", "")),
        )
        return result.to_dict()

    def diagnostics(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLitePlanStore(repo)
            latest = store.get_latest(self.account_id)
            return {
                "service": "student-execution-os",
                "version": __version__,
                "schema_version": repo.schema_version(),
                "server_revision": repo.get_server_revision(self.account_id),
                "account_binding": self.binding,
                "principal_binding": self.binding,
                "latest_plan": None if latest is None else {
                    "id": latest.id,
                    "plan_revision": latest.plan_revision,
                    "input_hash": latest.input_hash,
                    "feasibility_status": latest.feasibility_status.value,
                    "generated_at": _jsonify(latest.generated_at),
                },
                "connector_health": self._source_health(repo),
                "recurring_template_count": repo.connection.execute(
                    "SELECT count(*) FROM recurring_templates WHERE account_id=?", (self.account_id,)
                ).fetchone()[0],
                "reminder_delivery_state": [
                    dict(row) for row in repo.connection.execute(
                        "SELECT delivery_state AS state,count(*) AS count FROM reminder_messages "
                        "WHERE account_id=? GROUP BY delivery_state ORDER BY delivery_state",
                        (self.account_id,),
                    ).fetchall()
                ],
                "external_capabilities": {
                    "fcm": "CONFIGURED" if provider_from_environment().configured else "UNCONFIGURED",
                    "llm": "CONFIGURED" if assistant_capabilities()["live_llm_provider"] else "UNCONFIGURED",
                    "routing": "UNCONFIGURED",
                    "oauth": "UNCONFIGURED",
                },
            }

    def create_recurring_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        start = _local_dt(payload.get("dtstart_local"))
        if start is None:
            raise ValueError("dtstart_local is required")
        location = payload.get("location_effect") or {}
        with self._repo() as repo:
            template = SQLiteRecurrenceRepository(repo).create_template(
                account_id=self.account_id,
                template_id=payload.get("id"),
                title=str(payload["title"]),
                description=payload.get("description"),
                category=ObligationCategory(payload.get("category", ObligationCategory.GENERAL.value)),
                importance=Importance(payload.get("importance", Importance.NORMAL.value)),
                dtstart_local=start,
                duration_minutes=int(payload["duration_minutes"]),
                recurrence_rule=str(payload["recurrence_rule"]),
                timezone_name=str(payload["timezone_name"]),
                attendance_policy=AttendancePolicy(payload.get("attendance_policy", AttendancePolicy.REQUIRED.value)),
                location_effect=LocationEffect(
                    kind=LocationEffectKind(location.get("kind", LocationEffectKind.NONE.value)),
                    origin_place_id=location.get("origin_place_id"),
                    destination_place_id=location.get("destination_place_id"),
                ),
                arrival_requirement_minutes=int(payload.get("arrival_requirement_minutes", 0)),
                actor=ActorCategory.USER_UI,
            )
            return self._recurring_template_payload(template)

    def override_recurring_occurrence(
        self, template_id: str, original_recurrence_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._repo() as repo:
            item = SQLiteRecurrenceRepository(repo).set_override(
                account_id=self.account_id, template_id=template_id,
                original_recurrence_id=original_recurrence_id,
                action=OccurrenceOverrideAction(payload["action"]),
                replacement_start_local=_local_dt(payload.get("replacement_start_local")),
                replacement_duration_minutes=payload.get("replacement_duration_minutes"),
                expected_version=int(payload.get("expected_version", 0)),
                actor=ActorCategory.USER_UI,
            )
            return _jsonify(item)

    def split_recurring_series(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            old, successor = SQLiteRecurrenceRepository(repo).split_this_and_future(
                account_id=self.account_id, template_id=template_id,
                original_recurrence_id=str(payload["original_recurrence_id"]),
                successor_id=str(payload.get("successor_id") or uuid4()),
                replacement_start_local=_local_dt(payload.get("replacement_start_local")),
                recurrence_rule=payload.get("recurrence_rule"),
                expected_version=int(payload["expected_version"]),
                actor=ActorCategory.USER_UI,
            )
            return {
                "old_template": self._recurring_template_payload(old),
                "successor_template": self._recurring_template_payload(successor),
            }

    def snooze_notification(self, notification_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        until = _dt(payload.get("until"))
        if until is None:
            raise ValueError("snooze until is required")
        with self._repo() as repo:
            store = ReminderStore(repo)
            item = store.message(self.account_id, notification_id)
            for task_id in item["task_ids"]:
                store.touch(self.account_id, task_id, self._now(), snooze_until=until)
            store.mark_acted(self.account_id, notification_id, "SNOOZE", self._now())
            return store.message(self.account_id, notification_id)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_effort = payload.get("estimated_total_effort_minutes")
        effort = None if raw_effort in (None, "") else int(raw_effort)
        raw_remaining = payload.get("remaining_effort_minutes", effort)
        remaining = None if raw_remaining in (None, "") else int(raw_remaining)
        with self._repo() as repo:
            task = repo.create_task(
                account_id=self.account_id,
                title=str(payload["title"]),
                description=payload.get("description"),
                category=ObligationCategory(payload.get("category", ObligationCategory.GENERAL.value)),
                importance=Importance(payload.get("importance", Importance.NORMAL.value)),
                estimated_total_effort_minutes=effort,
                estimated_total_effort_low_minutes=payload.get("estimated_total_effort_low_minutes"),
                estimated_total_effort_high_minutes=payload.get("estimated_total_effort_high_minutes"),
                remaining_effort_minutes=remaining,
                remaining_effort_low_minutes=payload.get("remaining_effort_low_minutes"),
                remaining_effort_high_minutes=payload.get("remaining_effort_high_minutes"),
                splittable=bool(payload.get("splittable", False)),
                min_chunk_minutes=payload.get("min_chunk_minutes"),
                max_chunk_minutes=payload.get("max_chunk_minutes"),
                actionable_from=_dt(payload.get("actionable_from")),
                target_at=_dt(payload.get("target_at")),
                actual_cutoff=_cutoff(payload.get("actual_cutoff")),
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task.obligation.id, self._now())
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
            if "estimated_total_effort_minutes" in payload:
                value = payload.get("estimated_total_effort_minutes")
                kwargs["estimated_total_effort_minutes"] = None if value is None else int(value)
            if "splittable" in payload:
                kwargs["splittable"] = bool(payload["splittable"])
            if "min_chunk_minutes" in payload:
                kwargs["min_chunk_minutes"] = payload.get("min_chunk_minutes")
            if "max_chunk_minutes" in payload:
                kwargs["max_chunk_minutes"] = payload.get("max_chunk_minutes")
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
            ReminderStore(repo).touch(self.account_id, task_id, self._now())
            return self._task(task)

    def activate_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            task = repo.activate_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task_id, self._now())
            return self._task(task)

    def defer_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        until = _dt(payload.get("until"))
        if until is None or until <= self._now():
            raise ValueError("defer until must be a future offset-aware instant")
        with self._repo() as repo:
            task = repo.update_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actionable_from=until,
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task_id, self._now(), snooze_until=until)
            return self._task(task)

    def start_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            current = repo.get_task(self.account_id, task_id)
            if current.obligation.version != int(payload["expected_version"]):
                from student_execution_os.domain.errors import VersionConflict
                raise VersionConflict("task version changed")
            result = SyncService(
                repo, account_id=self.account_id, principal_id=self.principal.principal_id, now=self._now()
            ).apply({
                "op_id": str(payload.get("op_id") or f"api-start-{uuid4()}"),
                "type": "task.start", "entity_id": task_id, "payload": {},
            })
            if result["status"] == "CONFLICT":
                raise ValueError(result.get("message") or result.get("code") or "task cannot be started")
            return result["entity"]

    def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        operations = payload.get("operations")
        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        with self._repo() as repo:
            service = SyncService(
                repo, account_id=self.account_id, principal_id=self.principal.principal_id, now=self._now()
            )
            return {
                "results": service.apply_batch(operations),
                "server_revision": repo.get_server_revision(self.account_id),
                "synced_at": _jsonify(self._now()),
            }

    def upload_attachment(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).upload(self.account_id, payload)

    def attachments(self, owner_kind: str, owner_id: str) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).list(self.account_id, owner_kind, owner_id)

    def download_attachment(self, attachment_id: str):
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).download(self.account_id, attachment_id)

    def unlink_attachment(self, link_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).unlink(
                self.account_id, link_id, int(payload["expected_version"])
            )

    def saved_views(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).list(self.account_id)

    def create_saved_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).create(self.account_id, payload)

    def update_saved_view(self, view_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).update(self.account_id, view_id, payload)

    def delete_saved_view(self, view_id: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).delete(self.account_id, view_id, expected_version)

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
            if repo.connection.execute("SELECT 1 FROM tasks WHERE obligation_id=?", (obligation_id,)).fetchone():
                ReminderStore(repo).touch(self.account_id, obligation_id, self._now())
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
        options = tuple(EventLocationOption(
            id=str(item["id"]), label=str(item["label"]),
            effect=LocationEffect(
                kind=LocationEffectKind((item.get("location_effect") or {}).get("kind", "NONE")),
                origin_place_id=(item.get("location_effect") or {}).get("origin_place_id"),
                destination_place_id=(item.get("location_effect") or {}).get("destination_place_id"),
            ),
        ) for item in payload.get("location_options", []))
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
                location_options=options,
                selected_location_option_id=payload.get("selected_location_option_id"),
            )
            return self._event(event)

    def select_event_location(self, event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            event = repo.select_event_location_option(
                account_id=self.account_id, obligation_id=event_id,
                option_id=str(payload["option_id"]), expected_version=int(payload["expected_version"]),
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

    def assistant_interpret(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAssistantService(
                repo, self.principal, provider=assistant_provider_from_environment()
            ).interpret(
                str(payload.get("text", "")), payload.get("context")
            )

    def assistant_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAssistantService(repo, self.principal).apply(payload)

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
