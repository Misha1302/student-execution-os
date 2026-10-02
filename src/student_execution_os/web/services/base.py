"""Shared context of the application services."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from student_execution_os.agent import AuthenticatedPrincipal
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.reminders import ReminderStore
from student_execution_os.recurrence import SQLiteRecurrenceRepository
from student_execution_os.planning import SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.planning.model import PlanningPolicy
from student_execution_os.planning.preference_store import derived_preference_windows
from student_execution_os.planning.outlook import SQLitePlanningProfileRepository, off_hours_constraints
from student_execution_os.work_routines import SQLiteWorkRoutineRepository

from .common import _occurrence_details, _jsonify, _dt


class ApplicationService:
    """Request-scoped context shared by every application owner.

    The host binds account and principal (never the client); every call opens a
    fresh SQLite adapter scoped to that account. Also holds the read-model kernel
    that several owners present the same way (planning snapshot, task/event views).
    """

    def __init__(self, database: str, *, account_id: str, principal: AuthenticatedPrincipal,
                 now: Callable[[], datetime], binding: str) -> None:
        self.database = database
        self.account_id = account_id
        self.binding = binding
        self.principal = principal
        self._now = now

    def _repo(self) -> SQLiteCanonicalRepository:
        repo = SQLiteCanonicalRepository(self.database, clock=FrozenClock(self._now()))
        repo.initialize()
        repo._require_account(self.account_id)
        return repo

    def _snapshot(self, repo: SQLiteCanonicalRepository, *, hours: int = 36, output_hours: int | None = None):
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("UiService clock must be timezone-aware")
        SQLiteWorkRoutineRepository(repo).ensure_horizon(
            self.account_id, now, now + timedelta(hours=hours)
        )
        profile = SQLitePlanningProfileRepository(repo).get(self.account_id)
        return build_planning_snapshot(
            SQLitePlanningStateSource(repo),
            account_id=self.account_id,
            analysis_horizon_start=now,
            analysis_horizon_end=now + timedelta(hours=hours),
            plan_output_horizon_end=now + timedelta(hours=output_hours or min(hours, 36)),
            policy=PlanningPolicy(
                version=f"daily-product-v1:{profile.version}:{profile.optional_event_policy}",
                optional_event_policy=profile.optional_event_policy,
            ),
            derived_constraints=lambda start, end: off_hours_constraints(profile, self.account_id, start, end),
            derived_preferences=derived_preference_windows(repo, profile, self.account_id),
            assume_attendance=profile.optional_event_policy == "FAIL_CLOSED",
        )

    @staticmethod
    def _task(task, *, risk=None, effective=None, remind_at: datetime | None = None,
              count: dict[str, Any] | None = None) -> dict[str, Any]:
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
            "effort_estimate_source": task.effort_estimate_source,
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
            "created_at": _jsonify(task.obligation.created_at),
            "remind_at": _jsonify(remind_at),
            "count_progress": count,
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
    def _event(event, *, lead: int | None = None, remind_at: datetime | None = None) -> dict[str, Any]:
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
            "duration_minutes": int((event.interval.ends_at - event.interval.starts_at).total_seconds() // 60),
            "remind_before_minutes": lead,
            "remind_at": _jsonify(remind_at),
            "canonical": True,
        }

    def _events_payload(self, repo, events) -> list[dict[str, Any]]:
        # Show each event as stored: the snapshot may plan an optional event as
        # attended (assume_attendance), but its attendance policy is the user's.
        stored = {row["id"] for row in repo.connection.execute(
            "SELECT id FROM obligations WHERE account_id=? AND kind='EVENT'", (self.account_id,))}
        # (series occurrences are not stored rows; they keep their own policy)
        events = [repo.get_event(self.account_id, e.obligation.id) if e.obligation.id in stored else e for e in events]
        leads = extras.event_leads(repo, self.account_id)
        reminders = ReminderStore(repo).pending_reminders(self.account_id)
        out = [self._event(e, lead=leads.get(e.obligation.id), remind_at=reminders.get(e.obligation.id)) for e in events]
        self._attach_series(repo, out)
        return out

    def _attach_series(self, repo, payloads: list[dict[str, Any]]) -> None:
        """Classes carry their series identity, room and teacher (after SOURCE/USER changes)."""
        occurrence_ids = [p["id"] for p in payloads if str(p["id"]).startswith("rec:")]
        extra_rows = {row["event_id"]: row for row in repo.connection.execute(
            "SELECT event_id,template_id FROM series_extra_events WHERE account_id=?", (self.account_id,))}
        event_details = {row["event_id"]: row for row in repo.connection.execute(
            "SELECT event_id,location_text,teacher FROM event_details WHERE account_id=?", (self.account_id,))}
        for payload in payloads:
            row = event_details.get(str(payload["id"]))
            if row is not None:
                payload["location_text"], payload["teacher"] = row["location_text"], row["teacher"]
        if not occurrence_ids and not extra_rows:
            return
        recurrence = SQLiteRecurrenceRepository(repo)
        templates = {t.id: t for t in recurrence.list_templates(self.account_id)}
        details: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            event_id = str(payload["id"])
            if event_id in extra_rows:
                row = extra_rows[event_id]
                template = templates.get(row["template_id"])
                payload["series"] = {"template_id": row["template_id"], "extra": True,
                                     "imported": bool(template and template.source_system_id)}
                payload["location_text"] = payload.get("location_text") or (template.location_text if template else None)
                payload["teacher"] = payload.get("teacher") or (template.teacher if template else None)
                continue
            if not event_id.startswith("rec:"):
                continue
            template = next((t for tid, t in templates.items() if event_id.startswith(f"rec:{tid}:")), None)
            if template is None:
                continue
            original = event_id[len(f"rec:{template.id}:"):]
            if template.id not in details:
                starts = [_dt(p["starts_at"]) for p in payloads if str(p["id"]).startswith(f"rec:{template.id}:")]
                window_start = min(starts) - timedelta(days=1)
                window_end = max(starts) + timedelta(days=1)
                details[template.id] = {item.original_recurrence_id: item for item in recurrence.expand(
                    account_id=self.account_id, template_id=template.id, horizon_start=window_start, horizon_end=window_end)}
            item = details[template.id].get(original)
            payload["series"] = {"template_id": template.id, "original_recurrence_id": original,
                                 "imported": template.source_system_id is not None}
            if item is not None:
                payload.update({key: value for key, value in _occurrence_details(item).items() if key in {"location_text", "teacher", "note", "changed_by"}})

    def _source_health(self, repo: SQLiteCanonicalRepository) -> list[dict[str, Any]]:
        rows = repo.connection.execute(
            "SELECT id,provider,health_status,last_successful_complete_sync_at,latest_failure_reason,version "
            "FROM connector_states WHERE account_id=? ORDER BY id",
            (self.account_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _worker_status(self, repo: SQLiteCanonicalRepository) -> dict[str, Any]:
        row = repo.connection.execute(
            "SELECT beat_at,detail_json FROM worker_heartbeats WHERE name='reminder-worker'"
        ).fetchone()
        if row is None:
            return {"state": "NEVER_SEEN", "last_beat_at": None, "push_configured": False}
        import json
        beat = datetime.fromisoformat(row["beat_at"])
        detail = json.loads(row["detail_json"] or "{}")
        state = "RUNNING" if datetime.now(timezone.utc) - beat < timedelta(minutes=3) else "STALE"
        return {"state": state, "last_beat_at": row["beat_at"], "push_configured": bool(detail.get("push_configured")),
                "push_provider": detail.get("push_provider")}
