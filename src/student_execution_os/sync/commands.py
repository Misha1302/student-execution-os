"""Idempotent user commands and the offline sync endpoint behind them.

Every task/event mutation from the clients — whether sent immediately or replayed
from the offline outbox — is an *operation* with a client-generated ``op_id``:

    {"op_id": "...", "type": "task.complete", "entity_id": "...", "payload": {...}}

The operation and its result are committed in one transaction together with a
``client_operations`` row, so a replay of the same ``op_id`` returns the recorded
result instead of applying the change twice.

Conflict model (single user, several devices, long offline periods):

* Field edits are *field-level last-writer-wins*: an edit carries only the fields
  the user changed and is applied to whatever the current server version is.
* Progress is a delta ("worked 30 min"), so progress logged on two devices adds up.
* Lifecycle operations carry intent and are checked against the current state:
  completing an already-completed task is a harmless NOOP; completing a task that
  was cancelled elsewhere is a CONFLICT that the client shows to the user rather
  than silently resurrecting or dropping anything.
* Operations the server cannot apply at all (validation, missing entity) are
  REJECTED with a reason; the client keeps them visible until the user dismisses.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .handlers import (
    CheckInCommandHandler,
    EventCommandHandler,
    ExecutionCommandHandler,
    NoteCommandHandler,
    PlaceCommandHandler,
    PlanningCommandHandler,
    ProjectCommandHandler,
    ReminderCommandHandler,
    RoutineCommandHandler,
    SeriesCommandHandler,
    TaskCommandHandler,
)
from .handlers.base import Handler
# Re-exported: the envelope and other packages use these names from here.
from .primitives import APPLIED, CONFLICT, NOOP, OPEN, REJECTED, _ID, Outcome, parse_cutoff, parse_instant

__all__ = ["APPLIED", "CONFLICT", "NOOP", "OPEN", "REJECTED", "Commands", "Outcome", "SyncService",
           "parse_cutoff", "parse_instant"]

MAX_BATCH = 100
_REMINDER_ACTIONS = {"task.start": "START", "execution.start": "START", "task.complete": "DONE", "reminder.snooze": "SNOOZE",
                     "task.defer": "RESCHEDULE", "task.update": "RESCHEDULE", "task.progress": "PROGRESS",
                     "reminder.done": "DONE", "reminder.ack": "DONE", "reminder.update": "RESCHEDULE",
                     "checkin.occurrence.done": "DONE", "checkin.occurrence.skip": "SKIP",
                     "checkin.occurrence.progress": "PROGRESS"}
# Operation prefixes of the entity kinds that keep their delete tombstones in
# deleted_entities (schema v31+); the entity id of these operations is the owner id.
_TOMBSTONED = {"checkin.": "CHECKIN", "reminder_series.": "REMINDER_SERIES", "place.": "PLACE",
               "location_trigger.": "LOCATION_TRIGGER"}


class Commands:
    """Routes each operation type to the one domain handler that owns it.

    Registration is explicit and checked: an operation type with two owners is a
    programming error, and an unknown type fails closed in ``run``. Handlers run
    inside the caller's transaction; replay, idempotency and result persistence
    stay in ``SyncService``.
    """

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, actor: ActorCategory, now: datetime) -> None:
        self.repo = repo
        self.account_id = account_id
        self.actor = actor
        self.now = now
        context = {"account_id": account_id, "actor": actor, "now": now}
        self.tasks = TaskCommandHandler(repo, **context)
        self.execution = ExecutionCommandHandler(repo, **context)
        self.projects = ProjectCommandHandler(repo, **context, tasks=self.tasks)
        self.routines = RoutineCommandHandler(repo, **context)
        self.planning = PlanningCommandHandler(repo, **context)
        self.notes = NoteCommandHandler(repo, **context)
        self.events = EventCommandHandler(repo, **context)
        self.series = SeriesCommandHandler(repo, **context, events=self.events)
        self.reminders = ReminderCommandHandler(repo, **context)
        self.checkins = CheckInCommandHandler(repo, **context)
        self.places = PlaceCommandHandler(repo, **context)
        self.handlers: dict[str, Handler] = {}
        for owner in (self.tasks, self.execution, self.projects, self.routines, self.planning, self.notes,
                      self.events, self.series, self.reminders, self.checkins, self.places):
            for op_type, handler in owner.operations().items():
                if op_type in self.handlers:
                    raise RuntimeError(f"operation type {op_type} has two owners")
                self.handlers[op_type] = handler

    def run(self, op_type: str, entity_id: str, payload: dict[str, Any]) -> Outcome:
        handler = self.handlers.get(op_type)
        if handler is None:
            raise ValidationError(f"unknown operation type {op_type}")
        if op_type.startswith("note.") and entity_id and self.notes._notes().is_deleted(self.account_id, entity_id):
            return Outcome(NOOP, {"kind": "NOTE", "id": entity_id, "deleted": True}, "DELETED", "note was deleted")
        for prefix, kind in _TOMBSTONED.items():
            if op_type.startswith(prefix) and entity_id and self.repo.connection.execute(
                    "SELECT 1 FROM deleted_entities WHERE account_id=? AND entity_kind=? AND entity_id=?",
                    (self.account_id, kind, entity_id)).fetchone() is not None:
                return Outcome(NOOP, {"kind": kind, "id": entity_id, "deleted": True}, "DELETED", "item was deleted")
            if op_type.startswith(prefix):
                return handler(entity_id, payload)
        if op_type.startswith("reminder.") and entity_id and self.reminders._reminders().is_deleted(self.account_id, entity_id):
            return Outcome(NOOP, {"kind": "REMINDER", "id": entity_id, "deleted": True}, "DELETED", "reminder was deleted")
        if entity_id and self.repo.is_deleted_obligation(self.account_id, entity_id):
            # Deleted on this or another device: a late offline operation (or a replayed
            # create) must neither fail loudly nor bring the item back.
            kind = "EVENT" if op_type.startswith("event.") else "TASK"
            return Outcome(NOOP, {"kind": kind, "id": entity_id, "deleted": True}, "DELETED", "item was deleted")
        return handler(entity_id, payload)

    def current_entity(self, kind: str, entity_id: str) -> dict[str, Any]:
        """The client view of one item, as an operation result would return it."""
        if kind == "REMINDER":
            return self.reminders._reminders().get(self.account_id, entity_id)
        if kind == "EVENT":
            return self.events._event_out(entity_id).entity
        return self.tasks._task_out(entity_id).entity


class SyncService:
    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, principal_id: str,
                 actor: ActorCategory = ActorCategory.USER_UI, now: datetime | None = None) -> None:
        self.repo = repo
        self.account_id = account_id
        self.principal_id = principal_id
        self.now = now or repo.clock.now()
        self.commands = Commands(repo, account_id=account_id, actor=actor, now=self.now)

    @staticmethod
    def _hash(op_type: str, entity_id: str, payload: dict[str, Any]) -> str:
        raw = json.dumps({"type": op_type, "entity_id": entity_id, "payload": payload}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def apply(self, op: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(op, dict):
            raise ValidationError("operation must be an object")
        op_id = str(op.get("op_id") or "")
        op_type = str(op.get("type") or "")
        entity_id = str(op.get("entity_id") or "")
        payload = op.get("payload") or {}
        if not _ID.match(op_id):
            raise ValidationError("op_id must be 8-128 safe characters")
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object")
        request_hash = self._hash(op_type, entity_id, payload)
        with self.repo._tx() as conn:
            previous = conn.execute(
                "SELECT request_hash,result_json FROM client_operations WHERE account_id=? AND op_id=?",
                (self.account_id, op_id),
            ).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    return {"op_id": op_id, "status": REJECTED, "code": "OP_ID_REUSED",
                            "message": "op_id was already used for a different operation", "replayed": True}
                result = json.loads(previous["result_json"])
                result["replayed"] = True
                return result
            # A button pressed on a reminder (in the app or on a notification) names the
            # reminder so it is recorded as answered; the hash above still covers it.
            payload = dict(payload)
            reminder_id = payload.pop("reminder_message_id", None)
            try:
                with self.repo._tx():  # savepoint: a failing command leaves no partial writes
                    outcome = self.commands.run(op_type, entity_id, payload)
                    if reminder_id and outcome.status in (APPLIED, NOOP) and op_type in _REMINDER_ACTIONS:
                        from student_execution_os.reminders.store import ReminderStore
                        ReminderStore(self.repo).mark_acted(self.account_id, str(reminder_id)[:64],
                                                            _REMINDER_ACTIONS[op_type], self.now)
            except EntityNotFound as exc:
                outcome = Outcome(REJECTED, None, "NOT_FOUND", str(exc))
            except VersionConflict as exc:
                # A race with another writer is a conflict to reconcile, not invalid input.
                outcome = Outcome(CONFLICT, None, "VERSION_CONFLICT", str(exc))
            except (DomainError, ValueError, KeyError, TypeError) as exc:
                outcome = Outcome(REJECTED, None, "VALIDATION_ERROR", str(exc) or type(exc).__name__)
            result = {
                "op_id": op_id, "type": op_type, "entity_id": entity_id, "status": outcome.status,
                "code": outcome.code, "message": outcome.message, "entity": outcome.entity,
                "server_revision": self.repo.get_server_revision(self.account_id), "replayed": False,
            }
            conn.execute(
                "INSERT INTO client_operations(account_id,op_id,op_type,request_hash,status,result_json,principal_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.account_id, op_id, op_type, request_hash, outcome.status, json.dumps(result, sort_keys=True),
                 self.principal_id, _iso(self.now)),
            )
            return result

    def apply_batch(self, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(ops, list):
            raise ValidationError("ops must be a list")
        if len(ops) > MAX_BATCH:
            raise ValidationError(f"at most {MAX_BATCH} operations per request")
        results = []
        for op in ops:
            try:
                results.append(self.apply(op))
            except ValidationError as exc:
                # A malformed envelope is rejected on its own (not recorded, since it
                # has no usable op_id) so it can never block the rest of the queue.
                op_id = str(op.get("op_id") or "") if isinstance(op, dict) else ""
                results.append({"op_id": op_id, "status": REJECTED, "code": "MALFORMED_OPERATION",
                                "message": str(exc), "entity": None, "replayed": False})
        return results
