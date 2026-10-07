"""Checklist steps inside a Task (schema v33)."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.subtasks import SQLiteSubtaskRepository

from ..primitives import APPLIED, NOOP, _ID, Outcome, _minutes, _title

from .base import CommandHandler, Handler


def _position(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("position must be a number")
    if not -1e12 < float(value) < 1e12:
        raise ValidationError("position is out of range")
    return float(value)


class SubtaskCommandHandler(CommandHandler):
    """Field edits are last-writer-wins; done/reopen carry intent and are idempotent."""

    def operations(self) -> dict[str, Handler]:
        return {
            "subtask.create": self.subtask_create,
            "subtask.update": self.subtask_update,
            "subtask.complete": self.subtask_complete,
            "subtask.reopen": self.subtask_reopen,
            "subtask.move": self.subtask_move,
            "subtask.delete": self.subtask_delete,
        }

    def _store(self) -> SQLiteSubtaskRepository:
        return SQLiteSubtaskRepository(self.repo)

    def _checked(self, subtask_id: str, payload: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        """``task_id`` may ride along (the device uses it to update lists offline); it must match."""
        unknown = set(payload) - allowed - {"task_id"}
        if unknown:
            raise ValidationError("subtask fields are not supported: " + ", ".join(sorted(unknown)))
        current = self._store().get(self.account_id, subtask_id)
        if payload.get("task_id") is not None and payload["task_id"] != current["task_id"]:
            raise ValidationError("task_id does not match the subtask")
        return current

    def subtask_create(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(subtask_id):
            raise ValidationError("subtask id must be a client-generated identifier (8-128 safe characters)")
        if set(payload) - {"task_id", "title", "effort_minutes", "position"}:
            raise ValidationError("subtask fields are not supported")
        store = self._store()
        owner = store.owner_of(subtask_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("subtask id is already in use")
            return Outcome(NOOP, store.get(self.account_id, subtask_id), "ALREADY_EXISTS")
        return Outcome(APPLIED, store.create(
            account_id=self.account_id, subtask_id=subtask_id, task_id=str(payload.get("task_id") or ""),
            title=_title(payload.get("title")),
            effort_minutes=_minutes(payload.get("effort_minutes"), "effort_minutes", allow_none=True),
            position=_position(payload.get("position")), actor=self.actor, now=self.now))

    def subtask_update(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        self._checked(subtask_id, payload, {"title", "effort_minutes"})
        fields: dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = _title(payload["title"])
        if "effort_minutes" in payload:
            fields["effort_minutes"] = _minutes(payload["effort_minutes"], "effort_minutes", allow_none=True)
        store = self._store()
        if not fields:
            return Outcome(NOOP, store.get(self.account_id, subtask_id), "NOTHING_TO_CHANGE")
        return Outcome(APPLIED, store.update(self.account_id, subtask_id, fields, action="UPDATE_SUBTASK",
                                             actor=self.actor, now=self.now))

    def subtask_complete(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        store = self._store()
        current = self._checked(subtask_id, payload, {"occurred_at"})
        if current["done"]:
            return Outcome(NOOP, current, "ALREADY_DONE")
        moment = self._execution_moment(payload)
        return Outcome(APPLIED, store.update(self.account_id, subtask_id, {"done_at": moment.isoformat()},
                                             action="COMPLETE_SUBTASK", actor=self.actor, now=self.now))

    def subtask_reopen(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        store = self._store()
        current = self._checked(subtask_id, payload, set())
        if not current["done"]:
            return Outcome(NOOP, current, "ALREADY_OPEN")
        return Outcome(APPLIED, store.update(self.account_id, subtask_id, {"done_at": None}, action="REOPEN_SUBTASK",
                                             actor=self.actor, now=self.now))

    def subtask_move(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        self._checked(subtask_id, payload, {"position"})
        if "position" not in payload:
            raise ValidationError("subtask.move needs position")
        return Outcome(APPLIED, self._store().update(self.account_id, subtask_id,
                                                     {"position": _position(payload["position"])},
                                                     action="MOVE_SUBTASK", actor=self.actor, now=self.now))

    def subtask_delete(self, subtask_id: str, payload: dict[str, Any]) -> Outcome:
        self._checked(subtask_id, payload, set())
        self._store().delete(self.account_id, subtask_id, actor=self.actor, now=self.now)
        return Outcome(APPLIED, {"kind": "SUBTASK", "id": subtask_id, "deleted": True})
