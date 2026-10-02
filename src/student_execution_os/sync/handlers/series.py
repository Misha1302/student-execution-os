"""Class series: the USER layer of recurring events."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    Importance,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.recurrence.source import set_event_details

from ..primitives import APPLIED, NOOP, _ID, Outcome, parse_instant, _minutes, _title, _description, _short_text, _override_json, _series_json

from .base import CommandHandler, Handler
from .events import EventCommandHandler


class SeriesCommandHandler(CommandHandler):
    """Class series: the USER layer of recurring events."""

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, actor: ActorCategory,
                 now: datetime, events: EventCommandHandler) -> None:
        super().__init__(repo, account_id=account_id, actor=actor, now=now)
        self.events = events

    def operations(self) -> dict[str, Handler]:
        return {
            "series.create": self.series_create,
            "series.split": self.series_split,
            "series.occurrence.cancel": self.series_occurrence_cancel,
            "series.occurrence.move": self.series_occurrence_move,
            "series.occurrence.update": self.series_occurrence_update,
            "series.occurrence.restore": self.series_occurrence_restore,
            "series.extra.create": self.series_extra_create,
            "series.holiday": self.series_holiday,
            "series.holiday.restore": self.series_holiday_restore,
        }

    _SERIES_CREATE = {"title", "description", "category", "importance", "dtstart_local", "duration_minutes",
                      "recurrence_rule", "timezone_name", "attendance_policy", "location_effect",
                      "arrival_requirement_minutes", "location_text", "teacher"}

    def _recurrence(self):
        from student_execution_os.recurrence import SQLiteRecurrenceRepository
        return SQLiteRecurrenceRepository(self.repo)

    def _series_template(self, template_id: Any):
        if not template_id:
            raise ValidationError("template_id is required")
        return self._recurrence().get_template(self.account_id, str(template_id))

    def _series_occurrence(self, payload: dict[str, Any], allowed: set[str]):
        unknown = set(payload) - allowed - {"template_id", "original_recurrence_id"}
        if unknown:
            raise ValidationError("occurrence fields are not supported: " + ", ".join(sorted(unknown)))
        template = self._series_template(payload.get("template_id"))
        original = str(payload.get("original_recurrence_id") or "")
        if not original:
            raise ValidationError("original_recurrence_id is required")
        return template, original

    def _occurrence_out(self, template_id: str, original: str) -> dict[str, Any]:
        from student_execution_os.recurrence import OverrideLayer
        store = self._recurrence()
        user = store.get_override(self.account_id, template_id, original, OverrideLayer.USER)
        source = store.get_override(self.account_id, template_id, original, OverrideLayer.SOURCE)
        return {"kind": "OCCURRENCE", "template_id": template_id, "original_recurrence_id": original,
                "user_override": None if user is None else _override_json(user),
                "source_override": None if source is None else _override_json(source)}

    def series_create(self, template_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(template_id):
            raise ValidationError("series id must be a client-generated identifier (8-128 safe characters)")
        unknown = set(payload) - self._SERIES_CREATE
        if unknown:
            raise ValidationError("series fields are not supported: " + ", ".join(sorted(unknown)))
        existing = self.repo.connection.execute("SELECT account_id FROM recurring_templates WHERE id=?", (template_id,)).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("series id is already in use")
            return Outcome(NOOP, _series_json(self._series_template(template_id)), "ALREADY_EXISTS")
        location = payload.get("location_effect") or {}
        template = self._recurrence().create_template(
            account_id=self.account_id, template_id=template_id,
            title=_title(payload.get("title")), description=_description(payload.get("description")),
            category=ObligationCategory(payload.get("category") or ObligationCategory.LESSON.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            dtstart_local=self._local_instant(payload.get("dtstart_local"), "dtstart_local"),
            duration_minutes=_minutes(payload.get("duration_minutes"), "duration_minutes") or 0,
            recurrence_rule=str(payload.get("recurrence_rule") or ""),
            timezone_name=str(payload.get("timezone_name") or "UTC"),
            attendance_policy=AttendancePolicy(payload.get("attendance_policy") or AttendancePolicy.REQUIRED.value),
            location_effect=LocationEffect(
                kind=LocationEffectKind(location.get("kind", "NONE")),
                origin_place_id=location.get("origin_place_id"),
                destination_place_id=location.get("destination_place_id"),
            ),
            arrival_requirement_minutes=int(payload.get("arrival_requirement_minutes") or 0),
            location_text=_short_text(payload.get("location_text"), "location_text"),
            teacher=_short_text(payload.get("teacher"), "teacher"),
            actor=self.actor,
        )
        return Outcome(APPLIED, _series_json(template))

    def series_split(self, successor_id: str, payload: dict[str, Any]) -> Outcome:
        """"From this class on": a new series from the chosen occurrence; history stays."""
        allowed = {"template_id", "original_recurrence_id", "starts_local", "recurrence_rule", "duration_minutes",
                   "title", "location_text", "teacher", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("series split fields are not supported: " + ", ".join(sorted(unknown)))
        if not _ID.match(successor_id):
            raise ValidationError("successor series id must be a client-generated identifier")
        template = self._series_template(payload.get("template_id"))
        if template.source_system_id:
            raise ValidationError("an imported series changes in its source; edit single classes instead")
        store = self._recurrence()
        existing = self.repo.connection.execute("SELECT account_id FROM recurring_templates WHERE id=?", (successor_id,)).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("series id is already in use")
            return Outcome(NOOP, _series_json(store.get_template(self.account_id, successor_id)), "ALREADY_EXISTS")
        _old, successor = store.split_this_and_future(
            account_id=self.account_id, template_id=template.id,
            original_recurrence_id=str(payload.get("original_recurrence_id") or ""), successor_id=successor_id,
            replacement_start_local=(self._local_instant(payload["starts_local"], "starts_local") if payload.get("starts_local") else None),
            recurrence_rule=payload.get("recurrence_rule") or None,
            expected_version=int(payload["expected_version"]) if payload.get("expected_version") is not None else None,
            actor=self.actor,
        )
        fields: dict[str, Any] = {}
        if payload.get("duration_minutes") is not None:
            fields["duration_minutes"] = _minutes(payload["duration_minutes"], "duration_minutes")
        if payload.get("title") is not None:
            fields["title"] = _title(payload["title"])
        for key in ("location_text", "teacher"):
            if key in payload:
                fields[key] = _short_text(payload[key], key)
        if fields:
            successor = store.update_template(account_id=self.account_id, template_id=successor_id, fields=fields, actor=self.actor)
        return Outcome(APPLIED, _series_json(successor))

    def series_occurrence_cancel(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        from student_execution_os.recurrence import OccurrenceOverrideAction, OverrideLayer
        template, original = self._series_occurrence(payload, {"note"})
        store = self._recurrence()
        current = store.get_override(self.account_id, template.id, original, OverrideLayer.USER)
        if current is not None and current.action is OccurrenceOverrideAction.CANCEL:
            return Outcome(NOOP, self._occurrence_out(template.id, original), "ALREADY_CANCELLED")
        store.set_override(account_id=self.account_id, template_id=template.id, original_recurrence_id=original,
                           action=OccurrenceOverrideAction.CANCEL, actor=self.actor,
                           note=_short_text(payload.get("note"), "note"))
        return Outcome(APPLIED, self._occurrence_out(template.id, original))

    def _merge_user_modify(self, template, original: str, changes: dict[str, Any]) -> Outcome:
        from student_execution_os.recurrence import OccurrenceOverrideAction, OverrideLayer
        store = self._recurrence()
        current = store.get_override(self.account_id, template.id, original, OverrideLayer.USER)
        merged = {"replacement_start_local": None, "replacement_duration_minutes": None, "replacement_title": None,
                  "location_text": None, "teacher": None, "note": None}
        if current is not None and current.action is OccurrenceOverrideAction.MODIFY:
            merged.update({key: getattr(current, key) for key in merged})
        merged.update(changes)
        if all(value is None for value in merged.values()):
            store.remove_override(account_id=self.account_id, template_id=template.id, original_recurrence_id=original, actor=self.actor)
            return Outcome(APPLIED, self._occurrence_out(template.id, original))
        if current is not None and current.action is OccurrenceOverrideAction.MODIFY and all(
                getattr(current, key) == value for key, value in merged.items()):
            return Outcome(NOOP, self._occurrence_out(template.id, original), "UNCHANGED")
        store.set_override(account_id=self.account_id, template_id=template.id, original_recurrence_id=original,
                           action=OccurrenceOverrideAction.MODIFY, actor=self.actor, **merged)
        return Outcome(APPLIED, self._occurrence_out(template.id, original))

    def series_occurrence_move(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        template, original = self._series_occurrence(payload, {"starts_local", "duration_minutes"})
        changes: dict[str, Any] = {"replacement_start_local": self._local_instant(payload.get("starts_local"), "starts_local")}
        if payload.get("duration_minutes") is not None:
            changes["replacement_duration_minutes"] = _minutes(payload["duration_minutes"], "duration_minutes")
        return self._merge_user_modify(template, original, changes)

    def series_occurrence_update(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        """Room, teacher, title or a note for one class. An empty value drops that change."""
        from student_execution_os.recurrence import OccurrenceOverrideAction, OverrideLayer
        template, original = self._series_occurrence(payload, {"title", "location_text", "teacher", "note"})
        current = self._recurrence().get_override(self.account_id, template.id, original, OverrideLayer.USER)
        if current is not None and current.action is OccurrenceOverrideAction.CANCEL:
            raise ValidationError("this class is cancelled; restore it before editing")
        names = {"title": "replacement_title", "location_text": "location_text", "teacher": "teacher", "note": "note"}
        changes = {names[key]: _short_text(value, key) for key, value in payload.items() if key in names}
        return self._merge_user_modify(template, original, changes)

    def series_occurrence_restore(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        template, original = self._series_occurrence(payload, set())
        removed = self._recurrence().remove_override(
            account_id=self.account_id, template_id=template.id, original_recurrence_id=original, actor=self.actor)
        return Outcome(APPLIED if removed else NOOP, self._occurrence_out(template.id, original), None if removed else "NOTHING_TO_RESTORE")

    def series_extra_create(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        """An extra class: a canonical event linked to its series (Today, plan, reminders)."""
        allowed = {"template_id", "starts_at", "ends_at", "title", "location_text", "teacher"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("extra class fields are not supported: " + ", ".join(sorted(unknown)))
        template = self._series_template(payload.get("template_id"))
        if self.repo.connection.execute("SELECT 1 FROM obligations WHERE id=?", (event_id,)).fetchone() is not None:
            return self.events.event_create(event_id, {})  # same id replay: ALREADY_EXISTS / id in use
        starts_at = parse_instant(payload.get("starts_at"), "starts_at")
        ends_at = parse_instant(payload.get("ends_at"), "ends_at")
        if starts_at is None:
            raise ValidationError("an extra class needs starts_at")
        ends_at = ends_at or starts_at + timedelta(minutes=template.duration_minutes)
        outcome = self.events.event_create(event_id, {
            "title": payload.get("title") or template.title, "description": template.description,
            "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "category": template.category.value,
            "importance": template.importance.value, "attendance_policy": template.attendance_policy.value,
        })
        self.repo.connection.execute(
            "INSERT INTO series_extra_events(account_id,event_id,template_id,created_at) VALUES (?,?,?,?)",
            (self.account_id, event_id, template.id, _iso(self.now)))
        set_event_details(self.repo, self.account_id, event_id, now=self.now,
                          location_text=_short_text(payload.get("location_text"), "location_text"),
                          teacher=_short_text(payload.get("teacher"), "teacher"), actor=self.actor)
        return outcome

    def _holiday_targets(self, payload: dict[str, Any]):
        from datetime import date
        unknown = set(payload) - {"from_date", "to_date", "template_ids"}
        if unknown:
            raise ValidationError("holiday fields are not supported: " + ", ".join(sorted(unknown)))
        try:
            first = date.fromisoformat(str(payload.get("from_date")))
            last = date.fromisoformat(str(payload.get("to_date") or payload.get("from_date")))
        except ValueError as exc:
            raise ValidationError("from_date and to_date must be local dates (YYYY-MM-DD)") from exc
        if last < first or (last - first).days > 366:
            raise ValidationError("holiday range must be 1-367 days, from_date <= to_date")
        store = self._recurrence()
        ids = payload.get("template_ids")
        templates = [self._series_template(t) for t in ids] if ids else store.list_templates(self.account_id)
        for template in templates:
            for original_local in store._iter_original_locals(template):
                day = original_local.date()
                if day > last:
                    break
                if day >= first:
                    yield template, original_local.isoformat()

    def series_holiday(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        """Days off: every class in the range is cancelled with reason HOLIDAY (undo: series.holiday.restore)."""
        from student_execution_os.recurrence import OccurrenceOverrideAction, OverrideLayer, OverrideReason
        store = self._recurrence()
        cancelled = 0
        for template, original in list(self._holiday_targets(payload)):
            current = store.get_override(self.account_id, template.id, original, OverrideLayer.USER)
            if current is not None and current.action is OccurrenceOverrideAction.CANCEL:
                continue
            store.set_override(account_id=self.account_id, template_id=template.id, original_recurrence_id=original,
                               action=OccurrenceOverrideAction.CANCEL, reason=OverrideReason.HOLIDAY, actor=self.actor)
            cancelled += 1
        return Outcome(APPLIED if cancelled else NOOP, {"kind": "HOLIDAY", "cancelled": cancelled})

    def series_holiday_restore(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        from student_execution_os.recurrence import OverrideLayer, OverrideReason
        store = self._recurrence()
        restored = 0
        for template, original in list(self._holiday_targets(payload)):
            current = store.get_override(self.account_id, template.id, original, OverrideLayer.USER)
            if current is not None and current.reason is OverrideReason.HOLIDAY:
                store.remove_override(account_id=self.account_id, template_id=template.id,
                                      original_recurrence_id=original, actor=self.actor)
                restored += 1
        return Outcome(APPLIED if restored else NOOP, {"kind": "HOLIDAY", "restored": restored})
