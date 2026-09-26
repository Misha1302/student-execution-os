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

from student_execution_os.domain.errors import (
    AuthorizationDenied,
    DomainError,
    EntityNotFound,
    ValidationError,
    VersionConflict,
)
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
from student_execution_os.groups.errors import ExternalOwnedField, GroupRateLimited
from student_execution_os.groups.model import IllegalTransition
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .serialize import event_payload, task_payload

APPLIED, NOOP, CONFLICT, REJECTED = "APPLIED", "NOOP", "CONFLICT", "REJECTED"
OPEN = {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
MAX_BATCH = 100
_REMINDER_ACTIONS = {"task.start": "START", "task.complete": "DONE", "reminder.snooze": "SNOOZE",
                     "task.defer": "RESCHEDULE", "task.update": "RESCHEDULE", "task.progress": "PROGRESS",
                     "reminder.done": "DONE", "reminder.ack": "DONE", "reminder.update": "RESCHEDULE"}


@dataclass(frozen=True)
class Outcome:
    status: str
    entity: dict[str, Any] | None = None
    code: str | None = None
    message: str | None = None
    # Returned to the caller once and never stored in the operation log (an invite
    # link's secret token, for example).
    transient: dict[str, Any] | None = None


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
            "task.archive": self.task_archive,
            "task.unarchive": self.task_unarchive,
            "task.restore": self.task_restore,
            "task.delete": self.task_delete,
            "reminder.snooze": self.reminder_snooze,
            "event.create": self.event_create,
            "event.update": self.event_update,
            "event.cancel": self.event_cancel,
            "event.reopen": self.event_reopen,
            "event.delete": self.event_delete,
            "reminder.create": self.reminder_create,
            "reminder.update": self.reminder_update,
            "reminder.done": self.reminder_done,
            "reminder.ack": self.reminder_ack,
            "reminder.cancel": self.reminder_cancel,
            "reminder.reopen": self.reminder_reopen,
            "reminder.delete": self.reminder_delete,
        }
        # Collaborative groups (schema v18) share this pipeline: the same op log,
        # transaction and replay semantics (groups/commands.py).
        from student_execution_os.groups.commands import GroupCommands
        self.groups = GroupCommands(repo, account_id=account_id, now=now)
        self.handlers.update(self.groups.handlers)

    def run(self, op_type: str, entity_id: str, payload: dict[str, Any], *, mutation_id: str | None = None) -> Outcome:
        handler = self.handlers.get(op_type)
        if handler is None:
            raise ValidationError(f"unknown operation type {op_type}")
        if op_type in self.groups.handlers:
            self.groups.mutation_id = mutation_id
            return handler(entity_id, payload)
        if op_type.startswith("reminder.") and entity_id and self._reminders().is_deleted(self.account_id, entity_id):
            return Outcome(NOOP, {"kind": "REMINDER", "id": entity_id, "deleted": True}, "DELETED", "reminder was deleted")
        if entity_id and self.repo.is_deleted_obligation(self.account_id, entity_id):
            # Deleted on this or another device: a late offline operation (or a replayed
            # create) must neither fail loudly nor bring the item back.
            kind = "EVENT" if op_type.startswith("event.") else "TASK"
            return Outcome(NOOP, {"kind": kind, "id": entity_id, "deleted": True}, "DELETED", "item was deleted")
        return handler(entity_id, payload)

    # ---- helpers ----------------------------------------------------------------------

    def _task(self, task_id: str):
        return self.repo.get_task(self.account_id, task_id)

    def _task_out(self, task_id: str, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        from student_execution_os.reminders.store import ReminderStore
        remind = ReminderStore(self.repo).remind_at(self.account_id, task_id)
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        return Outcome(status, task_payload(self._task(task_id), remind_at=remind, count=count), code, message)

    def _event_out(self, event_id: str, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        from student_execution_os.reminders.store import ReminderStore
        return Outcome(status, event_payload(
            self.repo.get_event(self.account_id, event_id),
            remind_before_minutes=extras.event_lead(self.repo, self.account_id, event_id),
            remind_at=ReminderStore(self.repo).remind_at(self.account_id, event_id),
        ), code, message)

    def _count(self, payload: dict[str, Any], current: dict[str, Any] | None) -> tuple[bool, dict[str, Any] | None]:
        """Counted progress from create/update payload fields; (changed, new value)."""
        if not any(key in payload for key in ("count_total", "count_done", "count_unit")):
            return False, current
        total = current["total"] if current else None
        if "count_total" in payload:
            raw = payload["count_total"]
            total = None if raw in (None, "", 0) else _minutes(raw, "count_total")
        if total is None:
            return True, None
        done = current["done"] if current else 0
        if "count_done" in payload:
            done = _minutes(payload["count_done"], "count_done", allow_zero=True)
        unit = extras.clean_unit(payload["count_unit"]) if "count_unit" in payload else (current or {}).get("unit")
        return True, {"total": total, "done": min(done, total), "unit": unit}

    def _save_count(self, task_id: str, value: dict[str, Any] | None) -> None:
        extras.set_progress_count(self.repo, self.account_id, task_id, total=None if value is None else value["total"],
                                  done=0 if value is None else value["done"], unit=None if value is None else value["unit"],
                                  now=self.now)

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
                  "reopen": self.repo.reopen_obligation, "archive": self.repo.archive_obligation,
                  "unarchive": self.repo.unarchive_obligation}[action]
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
        prepares = None
        if payload.get("prepares"):
            # "Create preparation" for a shared assessment / "take on" a shared deadline:
            # an ordinary personal task, linked in the member's private overlay only.
            from student_execution_os.groups.commands import validate_link
            prepares = validate_link(payload["prepares"])
            if payload.get("actual_cutoff") is None:
                payload = {**payload, "actual_cutoff": self._shared_due(*prepares)}
            if not payload.get("importance"):
                # A preparation inherits how critical the assessment is *for this member*.
                from student_execution_os.groups.projection import PersonalProjection
                from student_execution_os.groups.model import Criticality, TASK_IMPORTANCE
                item = PersonalProjection(self.repo, self.account_id, self.now).item(*prepares)
                if item is not None and "criticality" in item:
                    payload = {**payload, "importance": TASK_IMPORTANCE[Criticality(item["criticality"]["effective"])]}
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
        changed, count = self._count(payload, None)
        if changed and count is not None:
            self._save_count(task_id, count)
        self._touch(task_id, remind_at=remind)
        if prepares is not None:
            self.groups.link_personal_task(*prepares, task.obligation.id)
        return self._task_out(task.obligation.id)

    def _shared_due(self, kind, entity_id: str) -> dict[str, Any]:
        """The deadline of a preparation task: the assessment's start / the shared deadline."""
        from student_execution_os.groups.model import SharedKind
        from student_execution_os.groups.projection import official_interval
        row = self.groups.groups.entity(kind, entity_id)
        if row is None:
            raise EntityNotFound("shared item not found")
        at = official_interval(self.repo, row)[0] if kind is SharedKind.SHARED_EVENT else datetime.fromisoformat(row["deadline"])
        return {"state": "KNOWN", "at": at.isoformat(), "boundary": "EXCLUSIVE"}

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
        "min_chunk_minutes", "max_chunk_minutes", "remind_at", "count_total", "count_done", "count_unit",
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
        count_changed, count = self._count(payload, extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id))
        if count_changed:
            self._save_count(task_id, count)
            remind_changed = True
        if not fields:
            return self._task_out(task_id, APPLIED if remind_changed else NOOP, None if remind_changed else "NOTHING_TO_CHANGE")
        activate = current.obligation.lifecycle_status is LifecycleStatus.DRAFT and estimate is not None
        self.repo.update_task(account_id=self.account_id, obligation_id=task_id,
                              expected_version=current.obligation.version, actor=self.actor, activate=activate, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_progress(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        """Worked time ("minutes", a delta) and/or counted progress ("count", a delta)."""
        count_delta = _minutes(payload.get("count"), "count", allow_none=True)
        minutes = _minutes(payload.get("minutes"), "minutes", allow_none=count_delta is not None) or 0
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED", "task is already closed; progress not recorded")
        fields: dict[str, Any] = {"last_progress_at": self.now}
        if current.started_at is None:
            fields["started_at"] = self.now
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        if count_delta is not None:
            if count is None:
                return self._task_out(task_id, REJECTED, "NO_COUNT", "the task has no counted progress")
            count = {**count, "done": min(count["total"], count["done"] + count_delta)}
            self._save_count(task_id, count)
            if not minutes and current.estimated_total_effort_minutes and current.remaining_effort_minutes is not None:
                # Nothing about time was said: the share of units left is the best estimate.
                left = -(-current.estimated_total_effort_minutes * (count["total"] - count["done"]) // count["total"])
                fields["remaining_effort_minutes"] = min(current.remaining_effort_minutes, left)
        if minutes and current.remaining_effort_minutes is not None:
            fields["remaining_effort_minutes"] = max(0, current.remaining_effort_minutes - minutes)
            if count is not None and count["done"] < count["total"]:
                # Units are still left: time alone does not finish the task.
                fields["remaining_effort_minutes"] = max(fields["remaining_effort_minutes"], 5)
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
            if current.obligation.completed_at is not None:
                # Completion subsumes a delayed "I started": nothing is left to apply
                # and nothing should be offered for a retry.
                return self._task_out(task_id, NOOP, "SUPERSEDED")
            # Starting work that was cancelled elsewhere is a real disagreement.
            return self._task_out(task_id, CONFLICT, "TASK_CANCELLED", "task was cancelled; reopen it first")
        if current.started_at is not None:
            self._touch(task_id)
            return self._task_out(task_id, NOOP, "ALREADY_STARTED")
        self._update(task_id, started_at=self.now, last_progress_at=self.now)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_complete(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id).obligation
        # An archived task keeps completed_at: archived-after-done is still done.
        if current.completed_at is not None:
            return self._task_out(task_id, NOOP, "ALREADY_COMPLETED")
        if current.lifecycle_status not in OPEN:
            return self._task_out(task_id, CONFLICT, "TASK_CANCELLED", "task was cancelled; reopen it first")
        self._transition(task_id, "complete")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_cancel(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id).obligation
        if current.completed_at is not None:
            return self._task_out(task_id, CONFLICT, "TASK_COMPLETED", "task was completed; reopen it first")
        if current.lifecycle_status not in OPEN:  # cancelled, or archived after cancelling
            return self._task_out(task_id, NOOP, "ALREADY_CANCELLED")
        self._transition(task_id, "cancel")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_reopen(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status in OPEN:
            return self._task_out(task_id, NOOP, "ALREADY_OPEN")
        self._transition(task_id, "reopen")
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        if count is not None and count["done"] >= count["total"] and payload.get("remaining_effort_minutes") is None:
            self._save_count(task_id, {**count, "done": max(0, count["total"] - 1)})
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

    def task_archive(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        status = self._task(task_id).obligation.lifecycle_status
        if status is LifecycleStatus.ARCHIVED:
            return self._task_out(task_id, NOOP, "ALREADY_ARCHIVED")
        if status in OPEN:
            # Put away something still open: it is "not doing it" first.
            self._transition(task_id, "cancel")
        self._transition(task_id, "archive")
        return self._task_out(task_id)

    def task_unarchive(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if self._task(task_id).obligation.lifecycle_status is not LifecycleStatus.ARCHIVED:
            return self._task_out(task_id, NOOP, "NOT_ARCHIVED")
        self._transition(task_id, "unarchive")
        return self._task_out(task_id)

    def task_restore(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        """Take a task out of the archive in one step.

        For the user the archive is one place, whether they said «не буду делать»
        (CANCELLED) or put a finished task away (ARCHIVED). Restoring returns a done
        task to «Выполнено» and anything else back to work — never into another
        archived state that would still show under «Архив».
        """
        status = self._task(task_id).obligation.lifecycle_status
        if status in OPEN:
            return self._task_out(task_id, NOOP, "ALREADY_OPEN")
        if status is LifecycleStatus.COMPLETED:
            return self._task_out(task_id, NOOP, "NOT_ARCHIVED")
        if status is LifecycleStatus.ARCHIVED:
            self._transition(task_id, "unarchive")
            if self._task(task_id).obligation.lifecycle_status is LifecycleStatus.COMPLETED:
                return self._task_out(task_id)
        return self.task_reopen(task_id, payload)

    def task_delete(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        self.repo.delete_obligation(account_id=self.account_id, obligation_id=task_id,
                                    expected_version=current.obligation.version, actor=self.actor)
        return Outcome(APPLIED, {"kind": "TASK", "id": task_id, "deleted": True})

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
        """«Напомни позже»: for a task, a standalone reminder, or an event."""
        until = parse_instant(payload.get("until"), "until")
        if until is None:
            minutes = _minutes(payload.get("minutes"), "minutes")
            until = self.now + timedelta(minutes=minutes)
        if self._reminders().exists(self.account_id, task_id):
            return self._snooze_reminder(task_id, until)
        if task_id.startswith(("shared-event:", "shared-obligation:")):
            # A reminder about a group's event/deadline: only the member's own reminder state moves.
            if until <= self.now:
                return Outcome(NOOP, {"kind": "SHARED", "id": task_id}, "SNOOZE_EXPIRED")
            self._touch(task_id, snooze_until=until, remind_at=until)
            return Outcome(APPLIED, {"kind": "SHARED", "id": task_id, "snoozed_until": until.isoformat()})
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED")
        if until <= self.now:
            return self._task_out(task_id, NOOP, "SNOOZE_EXPIRED")
        # Snooze means "remind me again then", not only "be quiet until then".
        self._touch(task_id, snooze_until=until, remind_at=until)
        return self._task_out(task_id)

    # ---- events -----------------------------------------------------------------------

    def _event_reminder(self, event_id: str, lead: int | None) -> None:
        """Keep the reminder moment of an event equal to "start minus lead"."""
        from student_execution_os.reminders.store import ReminderStore
        extras.set_event_lead(self.repo, self.account_id, event_id, lead)
        event = self.repo.get_event(self.account_id, event_id)
        remind = None
        if lead is not None and event.obligation.lifecycle_status in OPEN:
            remind = event.interval.starts_at - timedelta(minutes=lead)
            if remind <= self.now:
                # Too late for the heads-up but still before the start: remind right away.
                remind = self.now + timedelta(minutes=1) if event.interval.starts_at > self.now + timedelta(minutes=1) else None
        ReminderStore(self.repo).set_remind_at(self.account_id, event_id, remind, self.now)

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
                       "attendance_policy", "remind_before_minutes"}

    def event_update(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - self._EVENT_EDITABLE
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        current = self.repo.get_event(self.account_id, event_id)
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
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status is LifecycleStatus.CANCELLED:
            return self._event_out(event_id, NOOP, "ALREADY_CANCELLED")
        if status not in OPEN:
            return self._event_out(event_id, CONFLICT, "EVENT_CLOSED")
        self._transition(event_id, "cancel")
        return self._event_out(event_id)

    def event_reopen(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        status = self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status
        if status in OPEN:
            return self._event_out(event_id, NOOP, "ALREADY_OPEN")
        self._transition(event_id, "reopen")
        lead = extras.event_lead(self.repo, self.account_id, event_id)
        if lead is not None:
            self._event_reminder(event_id, lead)
        return self._event_out(event_id)

    def event_delete(self, event_id: str, payload: dict[str, Any]) -> Outcome:
        current = self.repo.get_event(self.account_id, event_id)
        self.repo.delete_obligation(account_id=self.account_id, obligation_id=event_id,
                                    expected_version=current.obligation.version, actor=self.actor)
        return Outcome(APPLIED, {"kind": "EVENT", "id": event_id, "deleted": True})


    # ---- standalone reminders ---------------------------------------------------------

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


class SyncService:
    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, principal_id: str,
                 actor: ActorCategory = ActorCategory.USER_UI, now: datetime | None = None,
                 channel: str = "direct") -> None:
        # channel "offline-queue": /api/v1/sync. Group-wide changes need the server's
        # confirmation, so they are not accepted from the replayed offline queue.
        self.channel = channel
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
        from student_execution_os.groups.commands import GROUP_WIDE_OPS
        if self.channel == "offline-queue" and op_type in GROUP_WIDE_OPS:
            return {"op_id": op_id, "type": op_type, "entity_id": entity_id, "status": REJECTED, "code": "ONLINE_ONLY",
                    "message": "group-wide changes are sent directly and need the server's confirmation",
                    "entity": None, "replayed": False}
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
            current_version = None
            try:
                with self.repo._tx():  # savepoint: a failing command leaves no partial writes
                    outcome = self.commands.run(op_type, entity_id, payload, mutation_id=op_id)
                    if reminder_id and outcome.status in (APPLIED, NOOP) and op_type in _REMINDER_ACTIONS:
                        from student_execution_os.reminders.store import ReminderStore
                        ReminderStore(self.repo).mark_acted(self.account_id, str(reminder_id)[:64],
                                                            _REMINDER_ACTIONS[op_type], self.now)
            except EntityNotFound as exc:
                outcome = Outcome(REJECTED, None, "NOT_FOUND", str(exc))
            except VersionConflict as exc:
                # A race with another writer is a conflict to reconcile, not invalid input.
                outcome = Outcome(CONFLICT, None, "VERSION_CONFLICT", str(exc))
                current_version = getattr(exc, "current_version", None)
            except GroupRateLimited:
                raise  # not recorded: the same mutation may be retried later
            except AuthorizationDenied as exc:
                outcome = Outcome(REJECTED, None, "FORBIDDEN", str(exc))
            except IllegalTransition as exc:
                outcome = Outcome(CONFLICT, None, "ILLEGAL_TRANSITION", str(exc))
            except ExternalOwnedField as exc:
                outcome = Outcome(REJECTED, None, "EXTERNAL_OWNED", str(exc))
            except (DomainError, ValueError, KeyError, TypeError) as exc:
                outcome = Outcome(REJECTED, None, "VALIDATION_ERROR", str(exc) or type(exc).__name__)
            result = {
                "op_id": op_id, "type": op_type, "entity_id": entity_id, "status": outcome.status,
                "code": outcome.code, "message": outcome.message, "entity": outcome.entity,
                "server_revision": self.repo.get_server_revision(self.account_id), "replayed": False,
            }
            if current_version is not None:
                result["current_version"] = current_version
            conn.execute(
                "INSERT INTO client_operations(account_id,op_id,op_type,request_hash,status,result_json,principal_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.account_id, op_id, op_type, request_hash, outcome.status, json.dumps(result, sort_keys=True),
                 self.principal_id, _iso(self.now)),
            )
            if outcome.transient:
                return {**result, "entity": {**(result["entity"] or {}), **outcome.transient}}
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
