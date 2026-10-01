"""Events, the calendar and places."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    EventLocationOption,
    Importance,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
)
from student_execution_os.recurrence import SQLiteRecurrenceRepository
from student_execution_os.planning import SQLitePlanningStateSource
from student_execution_os.travel import SQLiteTravelRepository

from .common import _occurrence_details, _jsonify, _dt, _local_iso

from .base import ApplicationService


class EventService(ApplicationService):
    """Events, the calendar and places."""

    def events(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return self._events_payload(repo, SQLitePlanningStateSource(repo).list_events(self.account_id))

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
            "location_text": template.location_text,
            "teacher": template.teacher,
            "source_system_id": template.source_system_id,
            "imported": template.source_system_id is not None,
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
                        **_occurrence_details(item),
                    })
            occurrences.sort(key=lambda item: (item["starts_at"], item["template_id"], item["original_recurrence_id"]))
            return {
                "events": [self._event(e) for e in source.list_events(self.account_id)],
                "recurring_templates": [self._recurring_template_payload(t) for t in templates],
                "occurrences": occurrences,
                "horizon_start": _jsonify(start),
                "horizon_end": _jsonify(end),
            }

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
