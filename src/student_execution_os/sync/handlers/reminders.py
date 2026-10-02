"""Standalone reminders and «напомни позже» for any item."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from student_execution_os.domain.errors import ValidationError

from ..primitives import APPLIED, NOOP, CONFLICT, OPEN, _ID, Outcome, parse_instant, _minutes, _title, _description

from .base import CommandHandler, Handler


class ReminderCommandHandler(CommandHandler):
    """Standalone reminders and «напомни позже» for any item."""

    def operations(self) -> dict[str, Handler]:
        return {
            "reminder.snooze": self.reminder_snooze,
            "reminder.create": self.reminder_create,
            "reminder.update": self.reminder_update,
            "reminder.done": self.reminder_done,
            "reminder.ack": self.reminder_ack,
            "reminder.cancel": self.reminder_cancel,
            "reminder.reopen": self.reminder_reopen,
            "reminder.delete": self.reminder_delete,
        }

    def reminder_snooze(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        """«Напомни позже»: for a task, a standalone reminder, or an event."""
        until = parse_instant(payload.get("until"), "until")
        if until is None:
            minutes = _minutes(payload.get("minutes"), "minutes")
            until = self.now + timedelta(minutes=minutes)
        if self._reminders().exists(self.account_id, task_id):
            return self._snooze_reminder(task_id, until)
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED")
        if until <= self.now:
            return self._task_out(task_id, NOOP, "SNOOZE_EXPIRED")
        # Snooze means "remind me again then", not only "be quiet until then".
        self._touch(task_id, snooze_until=until, remind_at=until)
        return self._task_out(task_id)

    def _reminders(self):
        from student_execution_os.reminders.standalone import SQLiteReminderRepository
        return SQLiteReminderRepository(self.repo)

    def _reminder_out(self, reminder: dict[str, Any], status: str = APPLIED, code: str | None = None,
                      message: str | None = None, *, before: dict[str, Any] | None = None) -> Outcome:
        from student_execution_os.reminders.standalone import has_alarm
        if status == APPLIED and (has_alarm(reminder["delivery"]) or (before and has_alarm(before["delivery"]))):
            # Other phones of the account reschedule their local alarms.
            from student_execution_os.reminders.store import ReminderStore
            ReminderStore(self.repo).signal_alarm_sync(self.account_id, self.now)
        return Outcome(status, reminder, code, message)

    def _reminder_fields(self, payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
        from student_execution_os.reminders.standalone import DELIVERIES
        fields: dict[str, Any] = {}
        if creating or "title" in payload:
            fields["title"] = _title(payload.get("title"))
        if "note" in payload:
            note = _description(payload.get("note"))
            if note and len(note) > 2000:
                raise ValidationError("note is longer than 2000 characters")
            fields["note"] = note
        if creating or "remind_at" in payload:
            at = parse_instant(payload.get("remind_at"), "remind_at")
            if at is None:
                raise ValidationError("remind_at is required")
            if at <= self.now - timedelta(hours=12):
                raise ValidationError("remind_at is too far in the past")
            fields["remind_at"] = at
        if creating or "delivery" in payload:
            delivery = str(payload.get("delivery") or "PUSH")
            if delivery not in DELIVERIES:
                raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
            fields["delivery"] = delivery
        for key in ("wake_check", "raise_volume"):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise ValidationError(f"{key} must be true or false")
                fields[key] = payload[key]
        return fields

    _REMINDER_EDITABLE = {"title", "note", "remind_at", "delivery", "wake_check", "raise_volume"}

    def reminder_create(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(reminder_id):
            raise ValidationError("reminder id must be a client-generated identifier (8-128 safe characters)")
        owner = self.repo.connection.execute("SELECT account_id FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if owner is not None:
            if owner["account_id"] != self.account_id:
                raise ValidationError("reminder id is already in use")
            return Outcome(NOOP, self._reminders().get(self.account_id, reminder_id), "ALREADY_EXISTS")
        fields = self._reminder_fields(payload, creating=True)
        reminder = self._reminders().create(
            self.account_id, reminder_id, title=fields["title"], remind_at=fields["remind_at"],
            delivery=fields["delivery"], note=fields.get("note"), wake_check=fields.get("wake_check", False),
            raise_volume=fields.get("raise_volume", False), obligation_id=payload.get("obligation_id") or None,
            actor=self._capture_actor(payload).value, now=self.now,
        )
        return self._reminder_out(reminder)

    def reminder_update(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - self._REMINDER_EDITABLE
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] == "CANCELLED":
            return Outcome(CONFLICT, before, "REMINDER_CANCELLED", "reminder was cancelled; restore it first")
        fields = self._reminder_fields(payload, creating=False)
        if not fields:
            return Outcome(NOOP, before, "NOTHING_TO_CHANGE")
        if "remind_at" in fields:
            repo.cancel_pending_messages(self.account_id, reminder_id, "RESCHEDULED")
        return self._reminder_out(repo.update(self.account_id, reminder_id, fields, self.now), before=before)

    def _snooze_reminder(self, reminder_id: str, until: datetime) -> Outcome:
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] not in ("SCHEDULED", "FIRED"):
            return Outcome(NOOP, before, "REMINDER_CLOSED")
        if until <= self.now:
            return Outcome(NOOP, before, "SNOOZE_EXPIRED")
        repo.cancel_pending_messages(self.account_id, reminder_id, "SNOOZED")
        return self._reminder_out(repo.snooze(self.account_id, reminder_id, until, self.now), before=before)

    def reminder_done(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] == "DONE":
            return Outcome(NOOP, before, "ALREADY_DONE")
        if before["status"] == "CANCELLED":
            return Outcome(CONFLICT, before, "REMINDER_CANCELLED", "reminder was cancelled")
        repo.cancel_pending_messages(self.account_id, reminder_id, "DONE")
        return self._reminder_out(repo.done(self.account_id, reminder_id, self.now), before=before)

    def reminder_ack(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        """«Я встал» (stage UP) and «Не сплю» after the awake check (stage AWAKE)."""
        stage = str(payload.get("stage") or "")
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] in ("DONE", "CANCELLED"):
            return Outcome(NOOP, before, "REMINDER_CLOSED")
        if stage == "UP" and before["acknowledged_at"]:
            return Outcome(NOOP, before, "ALREADY_UP")
        repo.cancel_pending_messages(self.account_id, reminder_id, "ACKNOWLEDGED")
        return self._reminder_out(repo.acknowledge(self.account_id, reminder_id, stage, self.now), before=before)

    def reminder_cancel(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] == "CANCELLED":
            return Outcome(NOOP, before, "ALREADY_CANCELLED")
        repo.cancel_pending_messages(self.account_id, reminder_id, "CANCELLED")
        return self._reminder_out(repo.cancel(self.account_id, reminder_id, self.now), before=before)

    def reminder_reopen(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        if before["status"] in ("SCHEDULED", "FIRED"):
            return Outcome(NOOP, before, "ALREADY_OPEN")
        return self._reminder_out(repo.reopen(self.account_id, reminder_id, self.now), before=before)

    def reminder_delete(self, reminder_id: str, payload: dict[str, Any]) -> Outcome:
        repo = self._reminders()
        before = repo.get(self.account_id, reminder_id)
        repo.delete(self.account_id, reminder_id, self.now)
        self._reminder_out({**before, "delivery": "PUSH"}, before=before)  # phones drop the alarm
        return Outcome(APPLIED, {"kind": "REMINDER", "id": reminder_id, "deleted": True})
