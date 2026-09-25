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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    CutoffBoundary,
    CutoffState,
    EventTimeSemantics,
    HardCutoff,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
    TemporalPrecision,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .serialize import event_payload, task_payload

APPLIED, NOOP, CONFLICT, REJECTED = "APPLIED", "NOOP", "CONFLICT", "REJECTED"
OPEN = {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
MAX_BATCH = 100
_REMINDER_ACTIONS = {"task.start": "START", "task.complete": "DONE", "reminder.snooze": "SNOOZE",
                     "task.defer": "RESCHEDULE", "task.update": "RESCHEDULE", "task.progress": "PROGRESS"}


@dataclass(frozen=True)
class Outcome:
    status: str
    entity: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None


def parse_instant(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a UTC offset")
    # The planner works on whole minutes.
    return parsed.replace(second=0, microsecond=0)


def parse_cutoff(payload: Any) -> HardCutoff:
    if payload is None:
        return HardCutoff.unknown()
    if not isinstance(payload, dict):
        raise ValidationError("actual_cutoff must be an object")
    state = CutoffState(payload.get("state", "UNKNOWN"))
    if state is CutoffState.ABSENT:
        return HardCutoff.absent()
    if state is CutoffState.UNKNOWN:
        return HardCutoff.unknown(TemporalPrecision(payload.get("precision", TemporalPrecision.UNKNOWN.value)))
    at = parse_instant(payload.get("at"), "actual_cutoff.at")
    if at is None:
        raise ValidationError("a KNOWN deadline requires at")
    return HardCutoff.known(at, CutoffBoundary(payload.get("boundary", CutoffBoundary.INCLUSIVE.value)))


def _minutes(value: Any, field: str, *, allow_zero: bool = False, allow_none: bool = False) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise ValidationError(f"{field} is required")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number of minutes") from exc
    if number < 0 or (number == 0 and not allow_zero) or number > 100_000:
        raise ValidationError(f"{field} is out of range")
    return number


def _title(value: Any) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise ValidationError("title is required")
    if len(title) > 300:
        raise ValidationError("title is longer than 300 characters")
    return title


def _description(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) > 5000:
        raise ValidationError("description is longer than 5000 characters")
    return text or None


class Commands:
    """The command handlers. They run inside the caller's transaction."""

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, actor: ActorCategory, now: datetime) -> None:
        self.repo = repo
        self.account_id = account_id
        self.actor = actor
        self.now = now
        self.handlers: dict[str, Callable[[str, dict[str, Any]], Outcome]] = {
            "task.create": self.task_create,
            "task.update": self.task_update,
            "task.activate": self.task_activate,
            "task.progress": self.task_progress,
            "task.start": self.task_start,
            "task.complete": self.task_complete,
            "task.cancel": self.task_cancel,
            "task.reopen": self.task_reopen,
            "task.defer": self.task_defer,
            "reminder.snooze": self.reminder_snooze,
            "event.create": self.event_create,
            "event.update": self.event_update,
            "event.cancel": self.event_cancel,
            "event.reopen": self.event_reopen,
        }

    def run(self, op_type: str, entity_id: str, payload: dict[str, Any]) -> Outcome:
        handler = self.handlers.get(op_type)
        if handler is None:
            raise ValidationError(f"unknown operation type {op_type}")
        return handler(entity_id, payload)

    # ---- helpers ----------------------------------------------------------------------

    def _task(self, task_id: str):
        return self.repo.get_task(self.account_id, task_id)

    def _task_out(self, task_id: str, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        from student_execution_os.reminders.store import ReminderStore
        remind = ReminderStore(self.repo).remind_at(self.account_id, task_id)
        return Outcome(status, task_payload(self._task(task_id), remind_at=remind), code, message)

    def _touch(self, task_id: str, *, snooze_until: datetime | None = None, remind_at: datetime | None = None) -> None:
        from student_execution_os.reminders.store import ReminderStore
        ReminderStore(self.repo).touch(self.account_id, task_id, self.now, snooze_until=snooze_until, remind_at=remind_at)

    def _remind_at(self, payload: dict[str, Any]) -> datetime | None:
        remind = parse_instant(payload.get("remind_at"), "remind_at")
        if remind is not None and remind <= self.now:
            raise ValidationError("remind_at must be in the future")
        return remind

    def _update(self, task_id: str, **fields) -> None:
        current = self._task(task_id)
        self.repo.update_task(account_id=self.account_id, obligation_id=task_id,
                              expected_version=current.obligation.version, actor=self.actor, **fields)

    def _transition(self, obligation_id: str, action: str) -> None:
        ob = self.repo.get_obligation(self.account_id, obligation_id)
        method = {"complete": self.repo.complete_obligation, "cancel": self.repo.cancel_obligation,
                  "reopen": self.repo.reopen_obligation}[action]
        method(account_id=self.account_id, obligation_id=obligation_id, expected_version=ob.version, actor=self.actor)

    # ---- tasks ------------------------------------------------------------------------

    def task_create(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(task_id):
            raise ValidationError("task id must be a client-generated identifier (8-128 safe characters)")
        exists = self.repo.connection.execute(
            "SELECT account_id FROM obligations WHERE id=?", (task_id,)
        ).fetchone()
        if exists is not None:
            if exists["account_id"] != self.account_id:
                raise ValidationError("task id is already in use")
            return Outcome(NOOP, task_payload(self._task(task_id)), "ALREADY_EXISTS", "task already exists")
        effort = _minutes(payload.get("estimated_total_effort_minutes"), "estimated_total_effort_minutes", allow_none=True)
        splittable = bool(payload.get("splittable", False))
        remind = self._remind_at(payload)
        task = self.repo.create_task(
            account_id=self.account_id, obligation_id=task_id,
            title=_title(payload.get("title")), description=_description(payload.get("description")),
            category=ObligationCategory(payload.get("category") or ObligationCategory.GENERAL.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            estimated_total_effort_minutes=effort, remaining_effort_minutes=effort,
            splittable=splittable,
            min_chunk_minutes=_minutes(payload.get("min_chunk_minutes"), "min_chunk_minutes", allow_none=True) if splittable else None,
            max_chunk_minutes=_minutes(payload.get("max_chunk_minutes"), "max_chunk_minutes", allow_none=True) if splittable else None,
            actionable_from=parse_instant(payload.get("actionable_from"), "actionable_from"),
            target_at=parse_instant(payload.get("target_at"), "target_at"),
            actual_cutoff=parse_cutoff(payload.get("actual_cutoff")),
            actor=self._capture_actor(payload),
        )
        self._touch(task_id, remind_at=remind)
        return self._task_out(task.obligation.id)

    def _capture_actor(self, payload: dict[str, Any]) -> ActorCategory:
        """A task confirmed from an Assistant preview keeps its LLM provenance.

        The reference must name a live preview batch of this account; otherwise (for
        example an offline capture replayed after the batch expired) the confirmed
        values are simply the user's own input.
        """
        batch_id = payload.get("assistant_batch_id")
        if not batch_id:
            return self.actor
        row = self.repo.connection.execute(
            "SELECT expires_at FROM assistant_batches WHERE account_id=? AND id=?", (self.account_id, str(batch_id))
        ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= self.now:
            return self.actor
        return ActorCategory.USER_VIA_LLM

    _EDITABLE = {
        "title", "description", "importance", "category", "estimated_total_effort_minutes",
        "remaining_effort_minutes", "actual_cutoff", "target_at", "actionable_from", "splittable",
        "min_chunk_minutes", "max_chunk_minutes", "remind_at",
    }

    def task_update(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - self._EDITABLE
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        current = self._task(task_id)
        fields: dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = _title(payload["title"])
        if "description" in payload:
            fields["description"] = _description(payload["description"])
        if "importance" in payload:
            fields["importance"] = Importance(payload["importance"])
        if "category" in payload:
            fields["category"] = ObligationCategory(payload["category"])
        if "actual_cutoff" in payload:
            fields["actual_cutoff"] = parse_cutoff(payload["actual_cutoff"])
        for key in ("target_at", "actionable_from"):
            if key in payload:
                fields[key] = parse_instant(payload[key], key)
        if "splittable" in payload:
            fields["splittable"] = bool(payload["splittable"])
        for key in ("min_chunk_minutes", "max_chunk_minutes"):
            if key in payload:
                fields[key] = _minutes(payload[key], key, allow_none=True)
        estimate = current.estimated_total_effort_minutes
        if "estimated_total_effort_minutes" in payload:
            estimate = _minutes(payload["estimated_total_effort_minutes"], "estimated_total_effort_minutes",
                                allow_none=current.obligation.lifecycle_status is LifecycleStatus.DRAFT)
            fields["estimated_total_effort_minutes"] = estimate
            if estimate is None:
                fields["remaining_effort_minutes"] = None
        if "remaining_effort_minutes" in payload and estimate is not None:
            fields["remaining_effort_minutes"] = _minutes(payload["remaining_effort_minutes"], "remaining_effort_minutes", allow_zero=True)
        if estimate is not None and "remaining_effort_minutes" not in fields:
            remaining = current.remaining_effort_minutes
            if remaining is None or remaining > estimate:
                fields["remaining_effort_minutes"] = estimate
        remind_changed = "remind_at" in payload
        if remind_changed:
            from student_execution_os.reminders.store import ReminderStore
            ReminderStore(self.repo).set_remind_at(self.account_id, task_id, self._remind_at(payload), self.now)
        if not fields:
            return self._task_out(task_id, APPLIED if remind_changed else NOOP, None if remind_changed else "NOTHING_TO_CHANGE")
        activate = current.obligation.lifecycle_status is LifecycleStatus.DRAFT and estimate is not None
        self.repo.update_task(account_id=self.account_id, obligation_id=task_id,
                              expected_version=current.obligation.version, actor=self.actor, activate=activate, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_progress(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        minutes = _minutes(payload.get("minutes"), "minutes")
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED", "task is already closed; progress not recorded")
        fields: dict[str, Any] = {"last_progress_at": self.now}
        if current.started_at is None:
            fields["started_at"] = self.now
        if current.remaining_effort_minutes is not None:
            fields["remaining_effort_minutes"] = max(0, current.remaining_effort_minutes - minutes)
            if current.remaining_effort_high_minutes is not None:
                fields["remaining_effort_high_minutes"] = max(fields["remaining_effort_minutes"], current.remaining_effort_high_minutes - minutes)
            if current.remaining_effort_low_minutes is not None:
                fields["remaining_effort_low_minutes"] = min(fields["remaining_effort_minutes"], max(0, current.remaining_effort_low_minutes - minutes))
        self._update(task_id, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_activate(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status is LifecycleStatus.ACTIVE:
            return self._task_out(task_id, NOOP, "ALREADY_ACTIVE")
        if current.obligation.lifecycle_status is not LifecycleStatus.DRAFT:
            return self._task_out(task_id, CONFLICT, "TASK_CLOSED")
        if current.estimated_total_effort_minutes is None:
            return self._task_out(task_id, REJECTED, "EFFORT_REQUIRED", "refine effort before activation")
        self.repo.activate_task(account_id=self.account_id, obligation_id=task_id,
                                expected_version=current.obligation.version, actor=self.actor)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_start(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, CONFLICT, "TASK_CLOSED",
                                  f"task is {current.obligation.lifecycle_status.value.lower()}")
        if current.started_at is not None:
            self._touch(task_id)
            return self._task_out(task_id, NOOP, "ALREADY_STARTED")
        self._update(task_id, started_at=self.now, last_progress_at=self.now)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_complete(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        status = self._task(task_id).obligation.lifecycle_status
        if status is LifecycleStatus.COMPLETED:
            return self._task_out(task_id, NOOP, "ALREADY_COMPLETED")
        if status is not LifecycleStatus.ACTIVE and status is not LifecycleStatus.DRAFT:
            return self._task_out(task_id, CONFLICT, "TASK_CANCELLED", "task was cancelled; reopen it first")
        self._transition(task_id, "complete")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_cancel(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        status = self._task(task_id).obligation.lifecycle_status
        if status is LifecycleStatus.CANCELLED:
            return self._task_out(task_id, NOOP, "ALREADY_CANCELLED")
        if status is LifecycleStatus.COMPLETED:
            return self._task_out(task_id, CONFLICT, "TASK_COMPLETED", "task was completed; reopen it first")
        self._transition(task_id, "cancel")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_reopen(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status in OPEN:
            return self._task_out(task_id, NOOP, "ALREADY_OPEN")
        self._transition(task_id, "reopen")
        reopened = self._task(task_id)
        remaining = _minutes(payload.get("remaining_effort_minutes"), "remaining_effort_minutes", allow_none=True)
        if reopened.estimated_total_effort_minutes is not None:
            if remaining is None and not reopened.remaining_effort_minutes:
                # "Not done after all" with nothing left on record: plan a modest block.
                remaining = min(reopened.estimated_total_effort_minutes, 30)
                if not reopened.splittable and reopened.min_chunk_minutes:
                    remaining = max(remaining, reopened.min_chunk_minutes)
            if remaining is not None:
                fields: dict[str, Any] = {"remaining_effort_minutes": remaining,
                                          "remaining_effort_low_minutes": None, "remaining_effort_high_minutes": None}
                if remaining > reopened.estimated_total_effort_minutes:
                    fields["estimated_total_effort_minutes"] = remaining
                self._update(task_id, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_defer(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        until = parse_instant(payload.get("until"), "until")
        if until is None:
            raise ValidationError("until is required")
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED")
        if until <= self.now:
            return self._task_out(task_id, NOOP, "DEFER_EXPIRED", "the postponement time has already passed")
        self._update(task_id, actionable_from=until)
        # "Not now": quiet until then, and come back with a prompt at that moment.
        self._touch(task_id, snooze_until=until, remind_at=until)
        return self._task_out(task_id)

    def reminder_snooze(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        until = parse_instant(payload.get("until"), "until")
        if until is None:
            minutes = _minutes(payload.get("minutes"), "minutes")
            until = self.now + timedelta(minutes=minutes)
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED")
        if until <= self.now:
            return self._task_out(task_id, NOOP, "SNOOZE_EXPIRED")
        # Snooze means "remind me again then", not only "be quiet until then".
        self._touch(task_id, snooze_until=until, remind_at=until)
        return self._task_out(task_id)

    # ---- events -----------------------------------------------------------------------

    def event_create(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(event_id):
            raise ValidationError("event id must be a client-generated identifier (8-128 safe characters)")
        exists = self.repo.connection.execute("SELECT account_id FROM obligations WHERE id=?", (event_id,)).fetchone()
        if exists is not None:
            if exists["account_id"] != self.account_id:
                raise ValidationError("event id is already in use")
            return Outcome(NOOP, event_payload(self.repo.get_event(self.account_id, event_id)), "ALREADY_EXISTS")
        location = payload.get("location_effect") or {}
        event = self.repo.create_event(
            account_id=self.account_id, obligation_id=event_id,
            title=_title(payload.get("title")), description=_description(payload.get("description")),
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=parse_instant(payload.get("starts_at"), "starts_at"),
            ends_at=parse_instant(payload.get("ends_at"), "ends_at"),
            category=ObligationCategory(payload.get("category") or ObligationCategory.GENERAL.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            attendance_policy=AttendancePolicy(payload.get("attendance_policy") or AttendancePolicy.REQUIRED.value),
            location_effect=LocationEffect(
                kind=LocationEffectKind(location.get("kind", "NONE")),
                origin_place_id=location.get("origin_place_id"),
                destination_place_id=location.get("destination_place_id"),
            ),
            arrival_requirement_minutes=int(payload.get("arrival_requirement_minutes") or 0),
            actor=self.actor,
        )
        return Outcome(APPLIED, event_payload(event))

    def event_update(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        current = self.repo.get_event(self.account_id, event_id)
        event = self.repo.update_fixed_event(
            account_id=self.account_id, obligation_id=event_id, expected_version=current.obligation.version,
            starts_at=parse_instant(payload.get("starts_at"), "starts_at") or current.interval.starts_at,
            ends_at=parse_instant(payload.get("ends_at"), "ends_at") or current.interval.ends_at,
            attendance_policy=AttendancePolicy(payload["attendance_policy"]) if payload.get("attendance_policy") else current.attendance_policy,
            actor=self.actor,
        )
        return Outcome(APPLIED, event_payload(event))

    def event_cancel(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status is LifecycleStatus.CANCELLED:
            return Outcome(NOOP, event_payload(self.repo.get_event(self.account_id, event_id)), "ALREADY_CANCELLED")
        if status not in OPEN:
            return Outcome(CONFLICT, event_payload(self.repo.get_event(self.account_id, event_id)), "EVENT_CLOSED")
        self._transition(event_id, "cancel")
        return Outcome(APPLIED, event_payload(self.repo.get_event(self.account_id, event_id)))

    def event_reopen(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status in OPEN:
            return Outcome(NOOP, event_payload(self.repo.get_event(self.account_id, event_id)), "ALREADY_OPEN")
        self._transition(event_id, "reopen")
        return Outcome(APPLIED, event_payload(self.repo.get_event(self.account_id, event_id)))


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
            except (DomainError, ValueError, KeyError, TypeError) as exc:
                outcome = Outcome(REJECTED, None, "VALIDATION", str(exc) or type(exc).__name__)
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
