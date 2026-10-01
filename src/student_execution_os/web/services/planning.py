"""Today, outlook, agenda, plan control and reflection: the planner's read models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import extras
from student_execution_os.execution import SQLiteExecutionStore
from student_execution_os.notes import SQLiteNoteRepository
from student_execution_os.persistence.metrics import SQLiteOperationalMetrics
from student_execution_os.reminders import ReminderStore
from student_execution_os.planning import (
    PlanningService,
    SQLitePlanStore,
    SQLitePlanningStateSource,
    build_planning_snapshot,
)
from student_execution_os.planning.model import PlanningPolicy
from student_execution_os.planning.outlook import (
    OFF_HOURS_PREFIX,
    SQLitePlanningProfileRepository,
    off_hours_constraints,
    overlap_minutes,
    planning_intervals,
)
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.travel import SQLiteTravelRepository
from student_execution_os.sync.commands import Commands
from student_execution_os.reflection import SQLiteReflectionStore

from .common import _jsonify, _dt, UPCOMING_HOURS

from .base import ApplicationService


class PlanningQueries(ApplicationService):
    """Today, outlook, agenda, plan control and reflection: the planner's read models."""

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
                derived_constraints=lambda start, end: off_hours_constraints(profile, self.account_id, start, end),
                assume_attendance=profile.optional_event_policy == "FAIL_CLOSED",
            )
            outcome = PlanningService().build(snapshot, now=self._now())
            windows = planning_intervals(profile, start_date, days)
            blocks = list(outcome.plan.blocks)
            constraints = [c for c in snapshot.constraints if not c.id.startswith(OFF_HOURS_PREFIX)]
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
            reminders = ReminderStore(repo).pending_reminders(self.account_id)
            counts = extras.progress_counts(repo, self.account_id)
            tasks = []
            for task in snapshot.tasks:
                tasks.append(self._task(
                    task,
                    risk=risks.get(task.obligation.id),
                    effective=reconciliation.get_effective_cutoff(self.account_id, task.obligation.id),
                    remind_at=reminders.get(task.obligation.id),
                    count=counts.get(task.obligation.id),
                ))
            events = self._events_payload(repo, snapshot.events)
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
            all_tasks = [self._task(task, remind_at=reminders.get(task.obligation.id), count=counts.get(task.obligation.id))
                         for task in SQLitePlanningStateSource(repo).list_tasks(self.account_id)]
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
            reflection = SQLiteReflectionStore(repo)
            local_date = reflection.local_date(self.account_id, self._now())
            daily_intent = reflection.intent(self.account_id, local_date)
            profile = SQLitePlanningProfileRepository(repo).get(self.account_id)
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(profile.timezone_name)
            local_day = datetime.fromisoformat(local_date).date()
            day_start = datetime.combine(local_day, datetime.min.time(), zone)
            day_end = day_start + timedelta(days=1)
            state_source = SQLitePlanningStateSource(repo)
            day_models = [
                *state_source.list_events(self.account_id),
                *state_source.list_recurring_events(self.account_id, day_start, day_end),
            ]
            day_events = self._events_payload(repo, [
                event for event in day_models
                if event.obligation.lifecycle_status.value == "ACTIVE"
                and event.interval.starts_at < day_end and day_start < event.interval.ends_at
            ])
            # "Soon" is a rolling window, not the local day: at 23:00 a class at 00:30 is next.
            now = self._now()
            soon_end = now + timedelta(hours=UPCOMING_HOURS)
            upcoming_models = [
                *state_source.list_events(self.account_id),
                *state_source.list_recurring_events(self.account_id, now - timedelta(days=1), soon_end),
            ]
            upcoming_events = sorted(self._events_payload(repo, [
                event for event in upcoming_models
                if event.obligation.lifecycle_status.value == "ACTIVE"
                and event.interval.starts_at < soon_end and now < event.interval.ends_at
            ]), key=lambda e: (e["starts_at"], e["id"]))
            inbox_notes = SQLiteNoteRepository(repo).list_unlinked(self.account_id, limit=3)
            windows = planning_intervals(profile, local_day, 1)
            capacity_minutes = sum(int((end - start).total_seconds() // 60) for start, end in windows)
            occupied_parts: list[tuple[datetime, datetime]] = []
            work_parts: list[tuple[datetime, datetime]] = []
            for block in outcome.plan.blocks:
                interval = (block.starts_at, block.ends_at)
                clipped = [
                    (max(interval[0], window[0]), min(interval[1], window[1]))
                    for window in windows if overlap_minutes(interval, window)
                ]
                occupied_parts.extend(clipped)
                if block.type.value == "WORK":
                    work_parts.extend(clipped)
            for constraint in snapshot.constraints:
                if constraint.id.startswith(OFF_HOURS_PREFIX):
                    continue
                interval = (constraint.interval.starts_at, constraint.interval.ends_at)
                occupied_parts.extend([
                    (max(interval[0], window[0]), min(interval[1], window[1]))
                    for window in windows if overlap_minutes(interval, window)
                ])
            planned_work_minutes = self._merged_minutes(work_parts)
            occupied_minutes = self._merged_minutes(occupied_parts)
            day_capacity = {
                "planning_capacity_minutes": capacity_minutes,
                "planned_work_minutes": planned_work_minutes,
                "occupied_minutes": occupied_minutes,
                "safe_reserve_minutes": max(0, capacity_minutes - occupied_minutes),
            }
            return {
                "now": _jsonify(self._now()),
                "local_date": local_date,
                "daily_intent": daily_intent,
                "day_capacity": day_capacity,
                "planning_effort_multipliers": {category: multiplier for category, multiplier in snapshot.effort_multipliers},
                "server_revision": snapshot.input_server_revision,
                "plan": plan_payload,
                "current_action": current_action,
                "interruptions": interruptions,
                "repair_actions": repair_actions,
                "needs_refinement": needs_refinement,
                "next_actions": _jsonify(outcome.next_actions),
                "tasks": tasks,
                "events": day_events,
                "upcoming_events": upcoming_events,
                "inbox_notes": inbox_notes,
                "travel": {
                    "transitions": transitions,
                    "unknown_reasons": list(snapshot.travel_projection.unknown_reasons),
                    "infeasible_reasons": list(snapshot.travel_projection.infeasible_reasons),
                },
                "source_health": self._source_health(repo),
                "active_execution": SQLiteExecutionStore(repo).active(self.account_id, self._now()),
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
            } for c in constraints if not c.id.startswith(OFF_HOURS_PREFIX)],
            # Sleep / off hours from the planning profile (derived, never stored).
            "off_hours": [{
                "starts_at": _jsonify(c.interval.starts_at),
                "ends_at": _jsonify(c.interval.ends_at),
            } for c in constraints if c.id.startswith(OFF_HOURS_PREFIX)],
        }

    def plan(self) -> dict[str, Any]:
        return self.today()["plan"]

    def agenda(self, days: int = 7) -> dict[str, Any]:
        """Day-by-day plan for the Plan screen: now until the end of the ``days``-th local day.

        Unlike Today it is not stored as the account's current plan; it is the same
        planner over a longer output horizon so the user can swipe through the week.
        """
        days = max(1, min(int(days), 14))
        with self._repo() as repo:
            from zoneinfo import ZoneInfo
            profile = SQLitePlanningProfileRepository(repo).get(self.account_id)
            zone = ZoneInfo(profile.timezone_name)
            now = self._now()
            local_end = datetime.combine(now.astimezone(zone).date() + timedelta(days=days), datetime.min.time(), zone)
            hours = max(1, int((local_end.astimezone(timezone.utc) - now).total_seconds() // 3600) + 1)
            snapshot = self._snapshot(repo, hours=hours, output_hours=hours)
            outcome = PlanningService().build(snapshot, now=now)
            risks = {r.task_id: r for r in outcome.risks}
            counts = extras.progress_counts(repo, self.account_id)
            tasks = [self._task(task, risk=risks.get(task.obligation.id), count=counts.get(task.obligation.id))
                     for task in snapshot.tasks]
            events = self._events_payload(repo, snapshot.events)
            return {
                "now": _jsonify(now), "days": days, "timezone": profile.timezone_name,
                "plan": self._plan_payload(outcome.plan, tasks, events, snapshot.constraints),
                "tasks": tasks,
            }

    def reflection(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(int(days), 31))
        with self._repo() as repo:
            from zoneinfo import ZoneInfo
            store = SQLiteReflectionStore(repo)
            zone = ZoneInfo(store.timezone_name(self.account_id))
            now = self._now()
            local_today = now.astimezone(zone).date()
            start_local = local_today - timedelta(days=days - 1)
            start = datetime.combine(start_local, datetime.min.time(), zone).astimezone(timezone.utc)
            data = store.review(self.account_id, start, now)
            data["days"] = days
            data["daily_intent"] = store.intent(self.account_id, local_today.isoformat())
            return data

    def reflection_daily(self, local_date: str) -> dict[str, Any]:
        with self._repo() as repo:
            from zoneinfo import ZoneInfo
            store = SQLiteReflectionStore(repo)
            zone = ZoneInfo(store.timezone_name(self.account_id))
            day = datetime.fromisoformat(local_date).date()
            start = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
            end = min(
                datetime.combine(day + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc),
                self._now(),
            )
            review = store.review(self.account_id, start, end) if start < end else {
                "start": _jsonify(start), "end": _jsonify(end),
                "timezone_name": store.timezone_name(self.account_id),
                "planned_work_minutes": 0, "actual_work_minutes": 0, "variance_minutes": 0,
                "completed_tasks": 0, "schedule_churn": 0, "carry_over_count": 0,
                "calibration_window_days": store.CALIBRATION_WINDOW_DAYS,
                "calibration": [], "most_underestimated": None,
            }
            review["local_date"] = local_date
            review["daily_intent"] = store.intent(self.account_id, local_date)
            return review

    @staticmethod
    def _constraint(constraint) -> dict[str, Any]:
        return {
            "id": constraint.id,
            "type": constraint.type.value,
            "starts_at": _jsonify(constraint.interval.starts_at),
            "ends_at": _jsonify(constraint.interval.ends_at),
            "obligation_id": constraint.obligation_id,
            "reason": constraint.reason,
            "version": constraint.version,
            "ownership": "CANONICAL",
        }

    def plan_constraints(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return [
                self._constraint(item)
                for item in SQLitePlanningStateSource(repo).list_time_constraints(self.account_id)
            ]

    def plan_control_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            raise ValueError("operation is required")
        kind = str(operation.get("type") or "")
        if kind not in {"constraint.create", "constraint.update", "constraint.delete"}:
            raise ValueError("preview only accepts constraint operations")
        entity_id = str(operation.get("entity_id") or "")
        op_payload = operation.get("payload") or {}
        if not isinstance(op_payload, dict):
            raise ValueError("operation payload must be an object")

        class _PreviewRollback(Exception):
            pass

        result: dict[str, Any] | None = None
        with self._repo() as repo:
            before_snapshot = self._snapshot(repo, hours=24 * 7, output_hours=24 * 7)
            before_outcome = PlanningService().build(before_snapshot, now=self._now())
            before_tasks = [self._task(task) for task in before_snapshot.tasks]
            before_events = self._events_payload(repo, before_snapshot.events)
            try:
                with repo._tx():
                    outcome = Commands(
                        repo,
                        account_id=self.account_id,
                        actor=ActorCategory.USER_UI,
                        now=self._now(),
                    ).run(kind, entity_id, dict(op_payload))
                    if outcome.status not in {"APPLIED", "NOOP"}:
                        raise ValueError(outcome.message or outcome.code or "preview operation could not be applied")
                    after_snapshot = self._snapshot(repo, hours=24 * 7, output_hours=24 * 7)
                    after_outcome = PlanningService().build(after_snapshot, now=self._now())
                    after_tasks = [self._task(task) for task in after_snapshot.tasks]
                    after_events = self._events_payload(repo, after_snapshot.events)
                    before_plan = self._plan_payload(
                        before_outcome.plan, before_tasks, before_events, before_snapshot.constraints
                    )
                    after_plan = self._plan_payload(
                        after_outcome.plan, after_tasks, after_events, after_snapshot.constraints
                    )
                    before_blocks = {
                        (b["type"], b.get("obligation_id"), b["starts_at"], b["ends_at"])
                        for b in before_plan["blocks"]
                    }
                    after_blocks = {
                        (b["type"], b.get("obligation_id"), b["starts_at"], b["ends_at"])
                        for b in after_plan["blocks"]
                    }
                    result = {
                        "operation": {
                            "type": kind,
                            "entity_id": entity_id,
                            "payload": op_payload,
                        },
                        "constraint": outcome.entity,
                        "before": before_plan,
                        "after": after_plan,
                        "delta": {
                            "added_blocks": len(after_blocks - before_blocks),
                            "removed_blocks": len(before_blocks - after_blocks),
                            "feasibility_changed": (
                                before_outcome.plan.feasibility_status.value
                                != after_outcome.plan.feasibility_status.value
                            ),
                        },
                    }
                    raise _PreviewRollback()
            except _PreviewRollback:
                pass
        assert result is not None
        return result
