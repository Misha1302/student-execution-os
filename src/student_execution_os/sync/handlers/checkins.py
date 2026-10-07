"""Tracked check-ins and recurring reminders (schema v31, ADR 0034)."""
from __future__ import annotations

from typing import Any

from student_execution_os.checkins import (
    CheckInKind,
    SQLiteCheckInRepository,
    Transition,
    clean_template_fields,
    occurrence_payload,
    template_payload,
)
from student_execution_os.domain.errors import ValidationError
from student_execution_os.reminders.series import SQLiteReminderSeriesRepository, series_payload

from ..primitives import APPLIED, CONFLICT, NOOP, _ID, Outcome, _description, _title, parse_instant

from .base import CommandHandler, Handler

_RULE_FIELDS = {"dtstart_local", "recurrence_rule", "timezone_name"}
_CHECKIN_FIELDS = {"title", "dose_text", "instructions", "target_quantity", "unit", "unit_effort_seconds",
                   "remind", "delivery", "followup_minutes", "window_minutes"}


def _version(payload: dict[str, Any]) -> int | None:
    value = payload.get("expected_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("expected_version must be an integer")
    return value


class CheckInCommandHandler(CommandHandler):
    """Check-in templates, their occurrences' outcomes, and reminder series."""

    def operations(self) -> dict[str, Handler]:
        return {
            "checkin.create": self.checkin_create,
            "checkin.update": self.checkin_update,
            "checkin.end": self.checkin_end,
            "checkin.delete": self.checkin_delete,
            "checkin.split": self.checkin_split,
            "checkin.occurrence.done": self.occurrence_done,
            "checkin.occurrence.skip": self.occurrence_skip,
            "checkin.occurrence.cancel": self.occurrence_cancel,
            "checkin.occurrence.reopen": self.occurrence_reopen,
            "checkin.occurrence.progress": self.occurrence_progress,
            "checkin.occurrence.move": self.occurrence_move,
            "reminder_series.create": self.series_create,
            "reminder_series.update": self.series_update,
            "reminder_series.end": self.series_end,
            "reminder_series.delete": self.series_delete,
            "reminder_series.split": self.series_split,
            "reminder_series.occurrence.skip": self.series_occurrence_skip,
            "reminder_series.occurrence.move": self.series_occurrence_move,
        }

    # ---- check-in templates -----------------------------------------------------------

    def _checkins(self) -> SQLiteCheckInRepository:
        return SQLiteCheckInRepository(self.repo)

    def _ensure(self) -> None:
        self._checkins().ensure_horizon(self.account_id, self.now)

    def _template_out(self, template_id: str, status: str = APPLIED, code: str | None = None) -> Outcome:
        return Outcome(status, template_payload(self._checkins().get_template(self.account_id, template_id)), code)

    def checkin_create(self, template_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(template_id):
            raise ValidationError("check-in id must be a client-generated identifier (8-128 safe characters)")
        unknown = set(payload) - _CHECKIN_FIELDS - _RULE_FIELDS - {"kind", "assistant_batch_id"}
        if unknown:
            raise ValidationError("check-in fields are not supported: " + ", ".join(sorted(unknown)))
        store = self._checkins()
        owner = store.owner_of(template_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("check-in id is already in use")
            return self._template_out(template_id, NOOP, "ALREADY_EXISTS")
        try:
            kind = CheckInKind(payload.get("kind") or CheckInKind.ROUTINE.value)
        except ValueError as exc:
            raise ValidationError("kind must be ROUTINE, MEDICATION or QUOTA") from exc
        fields = clean_template_fields({key: payload[key] for key in _CHECKIN_FIELDS if key in payload},
                                       kind=kind, creating=True)
        store.create_template(
            account_id=self.account_id, template_id=template_id, kind=kind,
            dtstart_local=self._local_instant(payload.get("dtstart_local"), "dtstart_local"),
            recurrence_rule=str(payload.get("recurrence_rule") or ""),
            timezone_name=str(payload.get("timezone_name") or "UTC"),
            actor=self._capture_actor(payload), fields=fields,
        )
        self._ensure()
        return self._template_out(template_id)

    def checkin_update(self, template_id: str, payload: dict[str, Any]) -> Outcome:
        fields = {key: value for key, value in payload.items() if key != "expected_version"}
        store = self._checkins()
        before = store.get_template(self.account_id, template_id)
        updated = store.update_template(self.account_id, template_id, fields, expected_version=_version(payload),
                                        actor=self.actor)
        self._ensure()
        return self._template_out(template_id, APPLIED if updated.version != before.version else NOOP,
                                  None if updated.version != before.version else "NOTHING_TO_CHANGE")

    def checkin_end(self, template_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"expected_version"}:
            raise ValidationError("checkin.end only accepts expected_version")
        _, changed = self._checkins().end_template(self.account_id, template_id,
                                                   expected_version=_version(payload), actor=self.actor)
        return self._template_out(template_id, APPLIED if changed else NOOP, None if changed else "ALREADY_ENDED")

    def checkin_delete(self, template_id: str, payload: dict[str, Any]) -> Outcome:
        self._checkins().delete_template(self.account_id, template_id, actor=self.actor)
        return Outcome(APPLIED, {"kind": "CHECKIN", "id": template_id, "deleted": True})

    def checkin_split(self, successor_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(successor_id):
            raise ValidationError("successor check-in id must be a client-generated identifier")
        allowed = {"template_id", "original_recurrence_id", "start_local", "recurrence_rule", "timezone_name",
                   "expected_version"} | _CHECKIN_FIELDS
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("check-in split fields are not supported: " + ", ".join(sorted(unknown)))
        store = self._checkins()
        if store.owner_of(successor_id) == self.account_id:
            return self._template_out(successor_id, NOOP, "ALREADY_EXISTS")
        template_id, original = self._identity(payload)
        previous, successor = store.split_this_and_future(
            account_id=self.account_id, template_id=template_id, original_recurrence_id=original,
            successor_id=successor_id, actor=self.actor, expected_version=_version(payload),
            start_local=self._local_instant(payload["start_local"], "start_local") if payload.get("start_local") else None,
            recurrence_rule=str(payload["recurrence_rule"]) if payload.get("recurrence_rule") else None,
            timezone_name=str(payload["timezone_name"]) if payload.get("timezone_name") else None,
            fields={key: payload[key] for key in _CHECKIN_FIELDS if key in payload},
        )
        self._ensure()
        return Outcome(APPLIED, {"previous": template_payload(previous), "successor": template_payload(successor)})

    # ---- occurrences ------------------------------------------------------------------

    @staticmethod
    def _identity(payload: dict[str, Any]) -> tuple[str, str]:
        template_id = str(payload.get("template_id") or "")
        original = str(payload.get("original_recurrence_id") or "")
        if not template_id or not original:
            raise ValidationError("a check-in occurrence needs template_id and original_recurrence_id")
        return template_id, original

    def _occurrence_identity(self, entity_id: str, payload: dict[str, Any]) -> tuple[str, str]:
        template_id = str(payload.get("template_id") or entity_id or "")
        original = str(payload.get("original_recurrence_id") or "")
        if entity_id and payload.get("template_id") and payload["template_id"] != entity_id:
            raise ValidationError("template_id does not match the operation entity")
        if not template_id or not original:
            raise ValidationError("a check-in occurrence needs template_id and original_recurrence_id")
        return template_id, original

    def _transition_out(self, result: Transition) -> Outcome:
        store = self._checkins()
        template = store.get_template(self.account_id, result.occurrence.template_id)
        message = None
        if result.status == CONFLICT:
            message = "this day already has another recorded outcome; reopen it first"
        return Outcome(result.status, occurrence_payload(store, template, result.occurrence), result.code, message)

    def _allowed(self, payload: dict[str, Any], extra: set[str]) -> None:
        unknown = set(payload) - {"template_id", "original_recurrence_id"} - extra
        if unknown:
            raise ValidationError("check-in occurrence fields are not supported: " + ", ".join(sorted(unknown)))

    def occurrence_done(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        """«Принял» / «Сделал»: the user's fact, with the moment it happened."""
        self._allowed(payload, {"occurred_at"})
        template_id, original = self._occurrence_identity(entity_id, payload)
        return self._transition_out(self._checkins().mark_done(
            self.account_id, template_id, original, actor=self.actor,
            occurred_at=self._execution_moment(payload)))

    def occurrence_skip(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        """«Не принял» / «Пропущу сегодня»."""
        self._allowed(payload, {"note"})
        template_id, original = self._occurrence_identity(entity_id, payload)
        return self._transition_out(self._checkins().mark_skipped(
            self.account_id, template_id, original, actor=self.actor, note=_description(payload.get("note"))))

    def occurrence_cancel(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        self._allowed(payload, set())
        template_id, original = self._occurrence_identity(entity_id, payload)
        return self._transition_out(self._checkins().cancel_occurrence(self.account_id, template_id, original,
                                                                       actor=self.actor))

    def occurrence_reopen(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        self._allowed(payload, set())
        template_id, original = self._occurrence_identity(entity_id, payload)
        return self._transition_out(self._checkins().reopen_occurrence(self.account_id, template_id, original,
                                                                       actor=self.actor))

    def occurrence_progress(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        """«Решил ещё 5»: a quantity delta, so two devices add up."""
        self._allowed(payload, {"count", "occurred_at"})
        template_id, original = self._occurrence_identity(entity_id, payload)
        count = payload.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValidationError("count must be a positive whole number")
        return self._transition_out(self._checkins().log_quantity(
            self.account_id, template_id, original, delta=count, actor=self.actor,
            occurred_at=self._execution_moment(payload)))

    def occurrence_move(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        """«Перенеси только сегодняшний приём на 22:00»."""
        self._allowed(payload, {"target_local"})
        template_id, original = self._occurrence_identity(entity_id, payload)
        target = self._local_instant(payload.get("target_local"), "target_local")
        return self._transition_out(self._checkins().move_occurrence(
            self.account_id, template_id, original, target_local=target, actor=self.actor))

    # ---- reminder series ----------------------------------------------------------------

    def _series(self) -> SQLiteReminderSeriesRepository:
        return SQLiteReminderSeriesRepository(self.repo)

    def _series_fields(self, payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        from student_execution_os.reminders.standalone import DELIVERIES
        fields: dict[str, Any] = {}
        if creating or "title" in payload:
            fields["title"] = _title(payload.get("title"))
        if "note" in payload:
            note = _description(payload.get("note"))
            if note and len(note) > 2000:
                raise ValidationError("note is longer than 2000 characters")
            fields["note"] = note
        if creating or "delivery" in payload:
            delivery = str(payload.get("delivery") or "PUSH")
            if delivery not in DELIVERIES:
                raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
            fields["delivery"] = delivery
        return fields

    def _series_out(self, series_id: str, status: str = APPLIED, code: str | None = None) -> Outcome:
        return Outcome(status, series_payload(self._series().get(self.account_id, series_id)), code)

    def series_create(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(series_id):
            raise ValidationError("series id must be a client-generated identifier (8-128 safe characters)")
        unknown = set(payload) - {"title", "note", "delivery", "assistant_batch_id"} - _RULE_FIELDS
        if unknown:
            raise ValidationError("reminder series fields are not supported: " + ", ".join(sorted(unknown)))
        store = self._series()
        owner = store.owner_of(series_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("series id is already in use")
            return self._series_out(series_id, NOOP, "ALREADY_EXISTS")
        fields = self._series_fields(payload, creating=True)
        store.create(account_id=self.account_id, series_id=series_id, title=fields["title"], note=fields.get("note"),
                     dtstart_local=self._local_instant(payload.get("dtstart_local"), "dtstart_local"),
                     recurrence_rule=str(payload.get("recurrence_rule") or ""),
                     timezone_name=str(payload.get("timezone_name") or "UTC"), delivery=fields["delivery"],
                     actor=self._capture_actor(payload))
        store.ensure_horizon(self.account_id, self.now)
        return self._series_out(series_id)

    def series_update(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - {"title", "note", "delivery", "expected_version"}
        if unknown:
            raise ValidationError("reminder series fields cannot be edited: " + ", ".join(sorted(unknown)))
        store = self._series()
        before = store.get(self.account_id, series_id)
        fields = self._series_fields(payload, creating=False)
        after = store.update(self.account_id, series_id, fields, expected_version=_version(payload), actor=self.actor)
        return self._series_out(series_id, APPLIED if after.version != before.version else NOOP)

    def series_end(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        _, changed = self._series().end(self.account_id, series_id, expected_version=_version(payload), actor=self.actor)
        return self._series_out(series_id, APPLIED if changed else NOOP, None if changed else "ALREADY_ENDED")

    def series_delete(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        self._series().delete(self.account_id, series_id, actor=self.actor)
        return Outcome(APPLIED, {"kind": "REMINDER_SERIES", "id": series_id, "deleted": True})

    def series_split(self, successor_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(successor_id):
            raise ValidationError("successor series id must be a client-generated identifier")
        unknown = set(payload) - {"series_id", "original_recurrence_id", "start_local", "recurrence_rule",
                                  "timezone_name", "expected_version", "title", "note", "delivery"}
        if unknown:
            raise ValidationError("reminder series split fields are not supported: " + ", ".join(sorted(unknown)))
        store = self._series()
        if store.owner_of(successor_id) == self.account_id:
            return self._series_out(successor_id, NOOP, "ALREADY_EXISTS")
        series_id = str(payload.get("series_id") or "")
        original = str(payload.get("original_recurrence_id") or "")
        if not series_id or not original:
            raise ValidationError("a series split needs series_id and original_recurrence_id")
        previous, successor = store.split(
            account_id=self.account_id, series_id=series_id, original_recurrence_id=original,
            successor_id=successor_id, actor=self.actor, expected_version=_version(payload),
            start_local=self._local_instant(payload["start_local"], "start_local") if payload.get("start_local") else None,
            recurrence_rule=str(payload["recurrence_rule"]) if payload.get("recurrence_rule") else None,
            timezone_name=str(payload["timezone_name"]) if payload.get("timezone_name") else None,
            fields=self._series_fields(payload, creating=False),
        )
        store.ensure_horizon(self.account_id, self.now)
        return Outcome(APPLIED, {"previous": series_payload(previous), "successor": series_payload(successor)})

    def _series_occurrence(self, entity_id: str, payload: dict[str, Any], extra: set[str]) -> str:
        unknown = set(payload) - {"original_recurrence_id"} - extra
        if unknown:
            raise ValidationError("series occurrence fields are not supported: " + ", ".join(sorted(unknown)))
        original = str(payload.get("original_recurrence_id") or "")
        if not original:
            raise ValidationError("a series occurrence needs original_recurrence_id")
        return self._series().require_occurrence(self.account_id, entity_id, original)

    def series_occurrence_skip(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        """Skip one day of a series (also one that is not materialized yet)."""
        from .reminders import ReminderCommandHandler
        reminder_id = self._series_occurrence(series_id, payload, set())
        return ReminderCommandHandler(self.repo, account_id=self.account_id, actor=self.actor,
                                      now=self.now).reminder_cancel(reminder_id, {})

    def series_occurrence_move(self, series_id: str, payload: dict[str, Any]) -> Outcome:
        from .reminders import ReminderCommandHandler
        reminder_id = self._series_occurrence(series_id, payload, {"remind_at"})
        at = parse_instant(payload.get("remind_at"), "remind_at")
        if at is None:
            raise ValidationError("remind_at is required")
        return ReminderCommandHandler(self.repo, account_id=self.account_id, actor=self.actor,
                                      now=self.now).reminder_update(reminder_id, {"remind_at": at.isoformat()})

