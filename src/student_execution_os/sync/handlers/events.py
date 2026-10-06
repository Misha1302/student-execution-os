"""Canonical events (including the user layer of imported events)."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
)
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import _iso

from ..serialize import event_payload

from ..primitives import APPLIED, NOOP, CONFLICT, OPEN, _ID, Outcome, parse_instant, _title, _description

from .base import CommandHandler, Handler


class EventCommandHandler(CommandHandler):
    """Canonical events (including the user layer of imported events)."""

    def operations(self) -> dict[str, Handler]:
        return {
            "event.create": self.event_create,
            "event.update": self.event_update,
            "event.cancel": self.event_cancel,
            "event.reopen": self.event_reopen,
            "event.delete": self.event_delete,
        }

    def _event_out(self, event_id: str, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        from student_execution_os.reminders.store import ReminderStore
        return Outcome(status, event_payload(
            self.repo.get_event(self.account_id, event_id),
            remind_before_minutes=extras.event_lead(self.repo, self.account_id, event_id),
            remind_at=ReminderStore(self.repo).remind_at(self.account_id, event_id),
        ), code, message)

    def _event_reminder(self, event_id: str, lead: int | None) -> None:
        """Keep the reminder moment of an event equal to "start minus lead" (one owner)."""
        from student_execution_os.reminders.events import sync_event_reminder
        sync_event_reminder(self.repo, self.account_id, event_id, self.now, by_user=True, lead=lead, lead_given=True)

    def event_create(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(event_id):
            raise ValidationError("event id must be a client-generated identifier (8-128 safe characters)")
        exists = self.repo.connection.execute("SELECT account_id FROM obligations WHERE id=?", (event_id,)).fetchone()
        if exists is not None:
            if exists["account_id"] != self.account_id:
                raise ValidationError("event id is already in use")
            return self._event_out(event_id, NOOP, "ALREADY_EXISTS")
        location = payload.get("location_effect") or {}
        starts_at = parse_instant(payload.get("starts_at"), "starts_at")
        ends_at = parse_instant(payload.get("ends_at"), "ends_at")
        if starts_at is None or ends_at is None:
            raise ValidationError("an event needs starts_at and ends_at")
        if ends_at <= starts_at:
            raise ValidationError("an event must end after it starts")
        lead = extras.parse_lead(payload.get("remind_before_minutes"))
        self.repo.create_event(
            account_id=self.account_id, obligation_id=event_id,
            title=_title(payload.get("title")), description=_description(payload.get("description")),
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=starts_at, ends_at=ends_at,
            category=ObligationCategory(payload.get("category") or ObligationCategory.GENERAL.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            attendance_policy=AttendancePolicy(payload.get("attendance_policy") or AttendancePolicy.REQUIRED.value),
            location_effect=LocationEffect(
                kind=LocationEffectKind(location.get("kind", "NONE")),
                origin_place_id=location.get("origin_place_id"),
                destination_place_id=location.get("destination_place_id"),
            ),
            arrival_requirement_minutes=int(payload.get("arrival_requirement_minutes") or 0),
            actor=self._capture_actor(payload),
        )
        if lead is not None:
            self._event_reminder(event_id, lead)
        return self._event_out(event_id)

    _EVENT_EDITABLE = {"title", "description", "category", "importance", "starts_at", "ends_at",
                       "attendance_policy", "remind_before_minutes", "location_effect", "arrival_requirement_minutes"}

    def _imported_event_identity(self, event_id: str):
        return self.repo.connection.execute(
            "SELECT * FROM external_identities WHERE account_id=? AND local_kind='EVENT' AND local_id=? "
            "AND external_recurrence_id=''",
            (self.account_id, event_id),
        ).fetchone()

    def _set_imported_event_user_cancelled(self, row, cancelled: bool) -> bool:
        if row is None or bool(row["user_cancelled"]) == cancelled:
            return False
        self.repo.connection.execute(
            "UPDATE external_identities SET user_cancelled=?,last_seen_at=? WHERE account_id=? "
            "AND source_system_id=? AND external_uid=? AND external_recurrence_id=''",
            (int(cancelled), _iso(self.now), self.account_id, row["source_system_id"], row["external_uid"]),
        )
        self.repo._record_change(
            self.repo.connection, account_id=self.account_id, entity_type="OBLIGATION", entity_id=row["local_id"],
            action="SET_IMPORTED_EVENT_USER_CANCELLED", actor=self.actor,
            payload={"cancelled": cancelled},
        )
        return True

    def event_update(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - self._EVENT_EDITABLE
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        if self._imported_event_identity(event_id) is not None and set(payload) - {"remind_before_minutes"}:
            return self._event_out(
                event_id, CONFLICT, "IMPORTED_EVENT_SOURCE_OWNED",
                "source-owned event fields change on schedule refresh; use a personal reminder",
            )
        current = self.repo.get_event(self.account_id, event_id)
        if "location_effect" in payload or "arrival_requirement_minutes" in payload:
            # Where it happens is its own change (the travel planner reads it).
            location = payload.get("location_effect") or {}
            if "location_effect" in payload and not isinstance(location, dict):
                raise ValidationError("location_effect must be an object")
            effect = current.location_effect if "location_effect" not in payload else LocationEffect(
                kind=LocationEffectKind(location.get("kind", "NONE")),
                origin_place_id=location.get("origin_place_id"),
                destination_place_id=location.get("destination_place_id"),
            )
            arrival = payload.get("arrival_requirement_minutes", current.arrival_requirement_minutes)
            if isinstance(arrival, bool) or not isinstance(arrival, int):
                raise ValidationError("arrival_requirement_minutes must be a whole number")
            if effect != current.location_effect or arrival != current.arrival_requirement_minutes:
                current = self.repo.update_event_location(
                    account_id=self.account_id, obligation_id=event_id, expected_version=current.obligation.version,
                    location_effect=effect, arrival_requirement_minutes=arrival, actor=self.actor,
                )
            payload = {k: v for k, v in payload.items() if k not in {"location_effect", "arrival_requirement_minutes"}}
            if not payload:
                return self._event_out(event_id)
        fields: dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = _title(payload["title"])
        if "description" in payload:
            fields["description"] = _description(payload["description"])
        if "category" in payload:
            fields["category"] = ObligationCategory(payload["category"])
        if "importance" in payload:
            fields["importance"] = Importance(payload["importance"])
        starts_at = parse_instant(payload.get("starts_at"), "starts_at") or current.interval.starts_at
        ends_at = parse_instant(payload.get("ends_at"), "ends_at") or current.interval.ends_at
        if "starts_at" in payload and "ends_at" not in payload:
            # Moving the start keeps the duration.
            ends_at = starts_at + (current.interval.ends_at - current.interval.starts_at)
        if ends_at <= starts_at:
            raise ValidationError("an event must end after it starts")
        policy = AttendancePolicy(payload["attendance_policy"]) if payload.get("attendance_policy") else None
        self.repo.update_fixed_event(
            account_id=self.account_id, obligation_id=event_id, expected_version=current.obligation.version,
            starts_at=starts_at, ends_at=ends_at, attendance_policy=policy, actor=self.actor, **fields,
        )
        lead = extras.event_lead(self.repo, self.account_id, event_id)
        if "remind_before_minutes" in payload:
            lead = extras.parse_lead(payload["remind_before_minutes"])
        if "remind_before_minutes" in payload or starts_at != current.interval.starts_at:
            self._event_reminder(event_id, lead)
        return self._event_out(event_id)

    def event_cancel(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        identity = self._imported_event_identity(event_id)
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status is LifecycleStatus.CANCELLED:
            marked = self._set_imported_event_user_cancelled(identity, True)
            return self._event_out(event_id, APPLIED if marked else NOOP, None if marked else "ALREADY_CANCELLED")
        if status not in OPEN:
            return self._event_out(event_id, CONFLICT, "EVENT_CLOSED")
        self._set_imported_event_user_cancelled(identity, True)
        self._transition(event_id, "cancel")
        return self._event_out(event_id)

    def event_reopen(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        identity = self._imported_event_identity(event_id)
        if identity is not None and (bool(identity["source_cancelled"]) or identity["state"] == "REMOVED"):
            return self._event_out(
                event_id, CONFLICT, "SOURCE_EVENT_CANCELLED",
                "the academic source still marks this event cancelled or removed",
            )
        unmarked = self._set_imported_event_user_cancelled(identity, False)
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status in OPEN:
            return self._event_out(event_id, APPLIED if unmarked else NOOP, None if unmarked else "ALREADY_OPEN")
        self._transition(event_id, "reopen")
        lead = extras.event_lead(self.repo, self.account_id, event_id)
        if lead is not None:
            self._event_reminder(event_id, lead)
        return self._event_out(event_id)

    def event_delete(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        if self._imported_event_identity(event_id) is not None:
            return self._event_out(
                event_id, CONFLICT, "IMPORTED_EVENT_SOURCE_OWNED",
                "disconnect or refresh the academic source instead of deleting its event",
            )
        current = self.repo.get_event(self.account_id, event_id)
        self.repo.delete_obligation(account_id=self.account_id, obligation_id=event_id,
                                    expected_version=current.obligation.version, actor=self.actor)
        return Outcome(APPLIED, {"kind": "EVENT", "id": event_id, "deleted": True})
