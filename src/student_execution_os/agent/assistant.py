from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import AuthorizationDenied, IdempotencyConflict, ValidationError, VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    HardCutoff,
    Importance,
    LifecycleStatus,
    ObligationCategory,
    UserTimeConstraintType,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.persistence.metrics import SQLiteOperationalMetrics

from .model import AgentCommand, AuthenticatedPrincipal
from .commands import parse_command, reschedule_change
from .disambiguation import judge
from .nlparse import parse_task
from .providers import ProviderUnavailable
from .read import SQLiteAssistantReadService
from .reliability import ReliabilityPolicy, ReliabilityTrace, sanitized_validation_feedback
from student_execution_os.planning.preferences import LIGHT_DAY_WORK_MINUTES, PreferenceKind, preference_from_payload

from .semantic import RelativeToAction, TemporalPrecision, parse_relative_to, resolve_temporal_transform


COMMANDS = {command.value for command in AgentCommand}
_CREATES = {AgentCommand.CREATE_TASK.value, AgentCommand.CREATE_EVENT.value, AgentCommand.CREATE_REMINDER.value, AgentCommand.CREATE_NOTE.value}
# Commands that close or put away something the user has: always confirmed by the user.
DESTRUCTIVE = {AgentCommand.COMPLETE_OBLIGATION.value, AgentCommand.CANCEL_OBLIGATION.value,
               AgentCommand.ARCHIVE_OBLIGATION.value}
_CREATE_TASK = re.compile(r"^(?:task|задача)\s*:\s*(.+)$", re.IGNORECASE)
_CREATE_EVENT = re.compile(r"^(?:event|событие)\s*:\s*(.+)$", re.IGNORECASE)
_DURATION = re.compile(r"(?:\b|\s)(\d{1,4})\s*(?:m|min|mins|minutes|мин|минут)\b", re.IGNORECASE)


class AssistantProvider(Protocol):
    name: str

    def interpret(self, text: str, context: dict[str, object]) -> list[dict[str, object]] | dict[str, Any]: ...


class DeterministicAssistantParser:
    name = "deterministic-local-v1"

    def interpret(self, text: str, context: dict[str, object]) -> list[dict[str, object]]:
        clean = " ".join(str(text or "").strip().split())
        if not clean:
            raise ValidationError("assistant input text is required")
        task = _CREATE_TASK.match(clean)
        if task:
            title = task.group(1).strip()
            duration = _DURATION.search(title)
            effort = int(duration.group(1)) if duration else None
            if duration:
                title = (title[:duration.start()] + title[duration.end():]).strip(" -|,")
            return [{
                "command": AgentCommand.CREATE_TASK.value,
                "payload": {"title": title, "estimated_total_effort_minutes": effort,
                            "remaining_effort_minutes": effort, "actual_cutoff": {"state": "UNKNOWN"},
                            "splittable": False},
                "confidence": 0.98, "unresolved_fields": [] if effort else ["estimated_total_effort_minutes"],
                "expected_version": None, "requires_confirmation": False,
            }]
        event = _CREATE_EVENT.match(clean)
        if event:
            parts = [part.strip() for part in event.group(1).split("|")]
            payload: dict[str, object] = {"title": parts[0], "location_effect": {"kind": "NONE"}}
            unresolved = []
            if len(parts) >= 3:
                payload.update({"starts_at": parts[1], "ends_at": parts[2]})
            else:
                unresolved.extend(["starts_at", "ends_at"])
            return [{"command": AgentCommand.CREATE_EVENT.value, "payload": payload, "confidence": 0.9,
                     "unresolved_fields": unresolved, "expected_version": None, "requires_confirmation": False}]
        # Commands about existing items ("готово эссе", "перенеси созвон на 18:00").
        now = _dt(str(context.get("now"))) if context.get("now") else None
        items = [*(context.get("obligations") or []), *(context.get("reminders") or [])]
        command = parse_command(clean, now=now or datetime.now(timezone.utc),
                                timezone_name=str(context.get("timezone") or "UTC"), items=items)
        if command is not None:
            if "target" in command["unresolved_fields"]:
                # Older clients expect the unresolved target under obligation_id.
                command["unresolved_fields"] = ["obligation_id" if f == "target" else f for f in command["unresolved_fields"]]
                command["payload"] = {"obligation_id": command["payload"].pop("target_text"), **command["payload"]}
                command["unresolved_fields"].append("expected_version")
            return [command]
        first_word = clean.casefold().split(" ", 1)[0].rstrip("?,")
        if first_word in {"что", "когда", "сколько", "какие", "почему", "what", "when", "why", "which"}:
            raise ValidationError("read question needs a typed assistant read query")
        if context.get("assistant_session"):
            raise ValidationError("conversational follow-up needs the language model")
        # Everyday phrasing ("в пятницу к шести сдать лабу, часа два, важно").
        parsed = parse_task(str(text), now=now or datetime.now(timezone.utc), timezone_name=str(context.get("timezone") or "UTC"))
        if not parsed.get("title"):
            raise ValidationError("input is not supported by the deterministic RU/EN parser")
        unresolved = [field for field in parsed.pop("unresolved") if field != "title"]
        parsed.pop("cutoff_time_assumed", None)
        parsed.pop("inferred_fields", None)
        if parsed.get("kind") == "REMINDER":
            return [{"command": AgentCommand.CREATE_REMINDER.value, "payload": _reminder_payload(parsed), "confidence": 0.85,
                     "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}]
        if parsed.get("kind") == "EVENT":
            payload = {key: parsed[key] for key in ("title", "description", "starts_at", "ends_at", "duration_minutes",
                                                         "remind_before_minutes", "category", "importance")
                       if parsed.get(key) is not None}
            return [{"command": AgentCommand.CREATE_EVENT.value, "payload": payload, "confidence": 0.85,
                     "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}]
        parsed.pop("kind", None)
        payload = {key: value for key, value in parsed.items() if value is not None or key in _NULLABLE_CAPTURE}
        return [{"command": AgentCommand.CREATE_TASK.value, "payload": payload,
                 "confidence": 0.8 if not unresolved else 0.6, "unresolved_fields": unresolved,
                 "expected_version": None, "requires_confirmation": False}]


def _reminder_payload(parsed: dict[str, object]) -> dict[str, object]:
    """CREATE_REMINDER payload of a local ``parse_task`` reading (known fields only)."""
    return {key: parsed[key] for key in ("title", "note", "remind_at", "delivery", "wake_check", "raise_volume")
            if parsed.get(key) is not None}


def _resolve_target(target: str, obligations: object) -> dict[str, Any] | None:
    """Exact id, or a unique case-insensitive title match among open obligations."""
    if not isinstance(obligations, list):
        return None
    wanted = target.casefold()
    by_id = [item for item in obligations if item.get("id") == target]
    if by_id:
        return by_id[0]
    by_title = [item for item in obligations if str(item.get("title", "")).casefold() == wanted]
    return by_title[0] if len(by_title) == 1 else None


_ACTION_REQUIRED = {"command", "payload", "confidence", "unresolved_fields", "expected_version", "requires_confirmation"}
_ACTION_KEYS = _ACTION_REQUIRED | {"field_provenance", "client_ref", "depends_on"}
# Every field a CREATE_TASK proposal may carry maps onto the canonical task.create
# command (sync/commands.py); anything else is rejected by validate_proposal.
_CREATE_TASK_FIELDS = {
    "title", "description", "category", "importance", "estimated_total_effort_minutes", "remaining_effort_minutes",
    "actual_cutoff", "target_at", "actionable_from", "remind_at", "splittable", "min_chunk_minutes", "max_chunk_minutes",
}
_NULLABLE_CAPTURE = {"estimated_total_effort_minutes"}
_TARGET = {"obligation_id", "reminder_id", "target_text"}
# A time the server derives from an earlier action of the same plan (never a sync field).
_RELATIVE = "relative_to"
_RELATIVE_FIELDS = {
    AgentCommand.CREATE_EVENT.value: ("starts_at", "ends_at"),
    AgentCommand.CREATE_TASK.value: ("actionable_from",),
    AgentCommand.CREATE_REMINDER.value: ("remind_at",),
}
_PREFERENCE_FIELDS = {"kind", "anchor", "target", "date_from", "date_until", "window_start", "window_end", "minutes",
                      "reason"}
_PAYLOAD_KEYS = {
    AgentCommand.CREATE_NOTE.value: {"content"},
    AgentCommand.CREATE_TASK.value: _CREATE_TASK_FIELDS | {_RELATIVE},
    AgentCommand.CREATE_EVENT.value: {"title", "description", "starts_at", "ends_at", "duration_minutes", "category", "importance",
                                      "attendance_policy", "location_effect", "arrival_requirement_minutes",
                                      "remind_before_minutes", _RELATIVE},
    AgentCommand.CREATE_REMINDER.value: {"title", "note", "remind_at", "delivery", "wake_check", "raise_volume", "obligation_id",
                                         _RELATIVE},
    AgentCommand.REFINE_TASK.value: {"obligation_id", "estimated_total_effort_minutes", "activate"},
    AgentCommand.LOG_PROGRESS.value: {"minutes", "count"} | _TARGET,
    AgentCommand.COMPLETE_OBLIGATION.value: set(_TARGET),
    AgentCommand.CANCEL_OBLIGATION.value: set(_TARGET),
    AgentCommand.ARCHIVE_OBLIGATION.value: set(_TARGET),
    AgentCommand.UPDATE_TASK.value: {"title", "description", "category", "importance", "estimated_total_effort_minutes",
                                     "actual_cutoff", "target_at", "actionable_from", "remind_at"} | _TARGET,
    AgentCommand.UPDATE_EVENT.value: {"title", "description", "starts_at", "ends_at", "remind_before_minutes",
                                      "attendance_policy"} | _TARGET,
    AgentCommand.UPDATE_REMINDER.value: {"title", "note", "remind_at", "delivery", "wake_check", "raise_volume"} | _TARGET,
    AgentCommand.RESCHEDULE.value: {"when", "keep_time", "temporal_transform"} | _TARGET,
    AgentCommand.SNOOZE.value: {"until"} | _TARGET,
    AgentCommand.CREATE_TIME_CONSTRAINT.value: {"type", "starts_at", "ends_at", "reason"},
    AgentCommand.CREATE_PLANNING_PREFERENCE.value: _PREFERENCE_FIELDS,
    AgentCommand.UNDO_LAST.value: set(),
}
_REQUIRED = {
    AgentCommand.CREATE_NOTE.value: ("content",),
    AgentCommand.CREATE_TASK.value: ("title",),
    AgentCommand.CREATE_EVENT.value: ("title", "starts_at", "ends_at"),
    AgentCommand.CREATE_REMINDER.value: ("title", "remind_at"),
    AgentCommand.REFINE_TASK.value: ("obligation_id", "estimated_total_effort_minutes"),
    AgentCommand.SNOOZE.value: ("until",),
    AgentCommand.CREATE_TIME_CONSTRAINT.value: ("type", "starts_at", "ends_at"),
    AgentCommand.CREATE_PLANNING_PREFERENCE.value: ("kind", "date_from"),
}
# Which kinds of item each command may address ("REMINDER" = a standalone reminder).
_TARGET_KINDS = {
    AgentCommand.REFINE_TASK.value: {"TASK"},
    AgentCommand.LOG_PROGRESS.value: {"TASK"},
    AgentCommand.COMPLETE_OBLIGATION.value: {"TASK", "REMINDER"},
    AgentCommand.CANCEL_OBLIGATION.value: {"TASK", "EVENT", "REMINDER"},
    AgentCommand.ARCHIVE_OBLIGATION.value: {"TASK"},
    AgentCommand.UPDATE_TASK.value: {"TASK"},
    AgentCommand.UPDATE_EVENT.value: {"EVENT"},
    AgentCommand.UPDATE_REMINDER.value: {"REMINDER"},
    AgentCommand.RESCHEDULE.value: {"TASK", "EVENT", "REMINDER"},
    AgentCommand.SNOOZE.value: {"TASK", "REMINDER"},
}


def _target_unresolved(unresolved: list[str]) -> bool:
    return bool({"target", "obligation_id", "reminder_id"} & set(unresolved))


def _positive_minutes(value: object, field: str, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 100_000:
        raise ValidationError(f"assistant proposal {field} must be a positive whole number of minutes")


def validate_proposal(raw: object, canonical: SQLiteCanonicalRepository, account_id: str, *,
                      timezone_name: str | None = None) -> dict[str, Any]:
    """Validate one provider action against the command schema and canonical state.

    Anything a model could get wrong — unknown commands or fields, wrong types,
    invented identifiers, bad dates — is rejected here, before the batch is stored,
    so it can neither be previewed nor applied. Fields the model honestly marks as
    unresolved are allowed to be missing; apply refuses them until refined.
    """
    if not isinstance(raw, dict) or not _ACTION_REQUIRED.issubset(raw) or set(raw) - _ACTION_KEYS:
        raise ValidationError("assistant provider returned an invalid typed action")
    command = raw["command"]
    payload = raw["payload"]
    if command not in COMMANDS or not isinstance(payload, dict):
        raise ValidationError("assistant provider returned an unknown action")
    unresolved = raw["unresolved_fields"]
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        raise ValidationError("assistant unresolved_fields must be a list of field names")
    if not isinstance(raw["requires_confirmation"], bool):
        raise ValidationError("assistant requires_confirmation must be a boolean")
    try:
        confidence = float(raw["confidence"])
    except (TypeError, ValueError) as exc:
        raise ValidationError("assistant confidence must be a number") from exc
    if not 0 <= confidence <= 1:
        raise ValidationError("assistant confidence must be in [0,1]")
    unknown = set(payload) - _PAYLOAD_KEYS[command]
    if unknown:
        raise ValidationError(f"assistant {command} payload has unsupported fields: {', '.join(sorted(unknown))}")
    field_provenance = raw.get("field_provenance") or {}
    if not isinstance(field_provenance, dict) or set(field_provenance) - set(payload):
        raise ValidationError("assistant field_provenance must name payload fields only")
    if not all(value in {"MODEL_EXPLICIT", "MODEL_INFERRED"} for value in field_provenance.values()):
        raise ValidationError("assistant field_provenance has an invalid value")
    field_provenance = {field: field_provenance.get(field, "MODEL_INFERRED") for field in payload}
    for field in _REQUIRED.get(command, ()):
        if payload.get(field) in (None, "") and field not in unresolved:
            raise ValidationError(f"assistant {command} payload lacks {field}")
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValidationError("assistant proposal title must be 1-300 characters")
    if command == AgentCommand.CREATE_NOTE.value and "content" in payload:
        content = payload["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 100_000:
            raise ValidationError("assistant note content must be nonempty text up to 100000 characters")
    if payload.get("description") is not None and (not isinstance(payload["description"], str) or len(payload["description"]) > 5000):
        raise ValidationError("assistant proposal description must be text up to 5000 characters")
    if command == AgentCommand.CREATE_TASK.value:
        _validate_task_fields(payload, canonical.clock.now())
    for field in ("estimated_total_effort_minutes", "remaining_effort_minutes"):
        if field in payload:
            _positive_minutes(payload[field], field, allow_none=command == AgentCommand.CREATE_TASK.value)
    if payload.get("minutes") is not None:
        _positive_minutes(payload["minutes"], "minutes")
    try:
        if "importance" in payload:
            Importance(payload["importance"])
        if "category" in payload:
            ObligationCategory(payload["category"])
        if "attendance_policy" in payload:
            AttendancePolicy(payload["attendance_policy"])
    except ValueError as exc:
        raise ValidationError(f"assistant proposal has an invalid enum value: {exc}") from exc
    if command == AgentCommand.CREATE_EVENT.value and payload.get("starts_at") and payload.get("ends_at"):
        try:
            starts, ends = _dt(str(payload["starts_at"])), _dt(str(payload["ends_at"]))
        except ValueError as exc:
            raise ValidationError("assistant event times must be ISO-8601 instants") from exc
        if starts is None or ends is None or starts.utcoffset() is None or ends.utcoffset() is None or ends <= starts:
            raise ValidationError("assistant event needs offset-aware starts_at < ends_at")
        duration = (ends - starts).total_seconds() / 60
        if duration.is_integer():
            duration = int(duration)
        supplied = payload.get("duration_minutes")
        if supplied is not None and (isinstance(supplied, bool) or not isinstance(supplied, int) or supplied != duration):
            raise ValidationError("assistant event duration_minutes must equal ends_at - starts_at")
        # The reviewed proposal is a full semantic object even when the provider
        # expressed duration only through its interval.
        payload["duration_minutes"] = duration
    if command == AgentCommand.CREATE_REMINDER.value:
        _validate_reminder_fields(payload, canonical.clock.now(), creating=True)
    if command == AgentCommand.UPDATE_REMINDER.value:
        _validate_reminder_fields(payload, canonical.clock.now(), creating=False)
    if command == AgentCommand.UPDATE_TASK.value:
        _validate_task_fields(payload, canonical.clock.now())
    if command == AgentCommand.UPDATE_EVENT.value:
        for field in ("starts_at", "ends_at"):
            if payload.get(field) is not None:
                _instant(payload[field], field)
        if payload.get("remind_before_minutes") is not None and (
                isinstance(payload["remind_before_minutes"], bool) or not isinstance(payload["remind_before_minutes"], int)
                or not 0 <= payload["remind_before_minutes"] <= 1440):
            raise ValidationError("assistant remind_before_minutes must be 0-1440")
    if command == AgentCommand.CREATE_EVENT.value and payload.get("remind_before_minutes") is not None:
        if isinstance(payload["remind_before_minutes"], bool) or not isinstance(payload["remind_before_minutes"], int) \
                or not 0 <= payload["remind_before_minutes"] <= 1440:
            raise ValidationError("assistant remind_before_minutes must be 0-1440")
    resolution: dict[str, str] | None = None
    if command == AgentCommand.RESCHEDULE.value:
        if payload.get("when") is None and payload.get("temporal_transform") is None and "when" not in unresolved:
            raise ValidationError("assistant RESCHEDULE payload needs when or temporal_transform")
        if payload.get("when") is not None and payload.get("temporal_transform") is not None:
            raise ValidationError("assistant RESCHEDULE must use when or temporal_transform, not both")
        if payload.get("when") is not None:
            resolved_when = _instant(payload["when"], "when")
            resolution = {"when": _iso(resolved_when), "precision": TemporalPrecision.EXACT.value,
                          "reason": "EXACT_USER_OR_MODEL_TIME"}
        if "keep_time" in payload and not isinstance(payload["keep_time"], bool):
            raise ValidationError("assistant keep_time must be a boolean")
    if command == AgentCommand.SNOOZE.value and payload.get("until") is not None:
        if _instant(payload["until"], "until") <= canonical.clock.now():
            raise ValidationError("assistant snooze time must be in the future")
    if command == AgentCommand.LOG_PROGRESS.value:
        if payload.get("minutes") is None and payload.get("count") is None and not {"minutes", "count"} & set(unresolved):
            raise ValidationError("assistant LOG_PROGRESS needs minutes or count")
        if payload.get("count") is not None:
            _positive_minutes(payload["count"], "count")
    if command == AgentCommand.CREATE_TIME_CONSTRAINT.value:
        try:
            constraint_type = UserTimeConstraintType(payload.get("type"))
        except ValueError:
            raise ValidationError("assistant time constraint has an invalid type") from None
        if constraint_type is UserTimeConstraintType.PINNED_WORK:
            raise ValidationError("assistant cannot create pinned work without an explicit task target")
        starts = _instant(payload.get("starts_at"), "starts_at")
        ends = _instant(payload.get("ends_at"), "ends_at")
        if ends <= starts or ends - starts > timedelta(days=14):
            raise ValidationError("assistant time constraint must be positive and at most 14 days")
        if payload.get("reason") is not None and (
                not isinstance(payload["reason"], str) or len(payload["reason"]) > 5000):
            raise ValidationError("assistant time constraint reason must be text up to 5000 characters")
    if command == AgentCommand.CREATE_PLANNING_PREFERENCE.value and not unresolved:
        if payload.get("kind") == PreferenceKind.WORK_LIMIT.value and payload.get("minutes") is None:
            # «сделай день полегче» without a number: the documented light-day budget.
            payload["minutes"] = LIGHT_DAY_WORK_MINUTES
            field_provenance["minutes"] = "MODEL_INFERRED"
        # The one preference parser (shared with preference.create) decides validity.
        preference_from_payload("assistant-preview", account_id, payload)
    if payload.get("target_text") is not None and (not isinstance(payload["target_text"], str) or len(payload["target_text"]) > 300):
        raise ValidationError("assistant target_text must be short text")
    if _RELATIVE in payload:
        parse_relative_to(payload[_RELATIVE])  # resolved against the plan by SQLiteAssistantService
    expected = raw["expected_version"]
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValidationError("assistant expected_version must be an integer")
    if command in _TARGET_KINDS:
        _validate_target(command, payload, unresolved, expected, canonical, account_id)
    elif command == AgentCommand.CREATE_REMINDER.value and payload.get("obligation_id"):
        if canonical.connection.execute("SELECT 1 FROM obligations WHERE account_id=? AND id=?",
                                        (account_id, str(payload["obligation_id"]))).fetchone() is None:
            raise ValidationError("assistant proposal references an unknown obligation")
    if command == AgentCommand.RESCHEDULE.value and payload.get("temporal_transform") is not None and not _target_unresolved(unresolved):
        target = target_of(canonical, account_id, payload)
        assert target is not None
        current = _target_moment(canonical, account_id, target[0], target[1])
        from student_execution_os.reminders import ReminderStore
        zone = timezone_name or ReminderStore(canonical).prefs(account_id).timezone_name
        resolved = resolve_temporal_transform(payload["temporal_transform"], current=current, timezone_name=zone)
        payload["when"] = _iso(resolved.when)
        resolution = {"when": _iso(resolved.when), "precision": resolved.precision.value,
                      "reason": resolved.reason, "timezone": zone}
    result = {"command": command, "payload": payload, "confidence": confidence,
            "unresolved_fields": list(unresolved), "expected_version": expected,
            "requires_confirmation": raw["requires_confirmation"], "field_provenance": field_provenance}
    if resolution is not None:
        result["resolution"] = resolution
    return result


def _instant(value: object, field: str) -> datetime:
    from student_execution_os.sync.commands import parse_instant
    if not isinstance(value, str):
        raise ValidationError(f"assistant {field} must be an ISO-8601 instant")
    parsed = parse_instant(value, field)
    assert parsed is not None
    return parsed


def target_of(canonical: SQLiteCanonicalRepository, account_id: str, payload: dict[str, Any]) -> tuple[str, str, int] | None:
    """(kind, id, version) of the item an action addresses, or None when it is unknown."""
    if payload.get("reminder_id"):
        row = canonical.connection.execute("SELECT version FROM reminders WHERE account_id=? AND id=?",
                                           (account_id, str(payload["reminder_id"]))).fetchone()
        return None if row is None else ("REMINDER", str(payload["reminder_id"]), int(row["version"]))
    if payload.get("obligation_id"):
        row = canonical.connection.execute("SELECT kind,version FROM obligations WHERE account_id=? AND id=?",
                                           (account_id, str(payload["obligation_id"]))).fetchone()
        return None if row is None else (row["kind"], str(payload["obligation_id"]), int(row["version"]))
    return None


def _target_moment(canonical: SQLiteCanonicalRepository, account_id: str, kind: str, entity_id: str) -> datetime:
    if kind == "EVENT":
        return canonical.get_event(account_id, entity_id).interval.starts_at
    if kind == "REMINDER":
        row = canonical.connection.execute(
            "SELECT remind_at FROM reminders WHERE account_id=? AND id=?", (account_id, entity_id),
        ).fetchone()
        if row is not None:
            return _dt(row["remind_at"])
    if kind == "TASK":
        row = canonical.connection.execute(
            "SELECT actual_cutoff_at,actionable_from,target_at FROM tasks WHERE account_id=? AND obligation_id=?",
            (account_id, entity_id),
        ).fetchone()
        if row is not None:
            for field in ("actual_cutoff_at", "actionable_from", "target_at"):
                if row[field]:
                    return _dt(row[field])
    raise ValidationError("assistant cannot shift an item without a current time")


def _validate_target(command: str, payload: dict[str, Any], unresolved: list[str], expected: object,
                     canonical: SQLiteCanonicalRepository, account_id: str) -> None:
    if _target_unresolved(unresolved):
        return  # the user picks the item in the preview; apply refuses until then
    if payload.get("obligation_id") and payload.get("reminder_id"):
        raise ValidationError("assistant action must address one item")
    if not payload.get("obligation_id") and not payload.get("reminder_id"):
        raise ValidationError(f"assistant {command} payload lacks obligation_id")
    target = target_of(canonical, account_id, payload)
    if target is None:
        # Never let a model address something that does not exist in this account.
        raise ValidationError("assistant proposal references an unknown obligation")
    if target[0] not in _TARGET_KINDS[command]:
        raise ValidationError(f"assistant {command} cannot address a {target[0].lower()}")
    if expected is None and "expected_version" not in unresolved:
        raise ValidationError("assistant proposal on an existing obligation needs expected_version")


def _validate_reminder_fields(payload: dict[str, Any], now: datetime, *, creating: bool) -> None:
    from student_execution_os.reminders.standalone import DELIVERIES
    if payload.get("remind_at") is not None and _instant(payload["remind_at"], "remind_at") <= now:
        raise ValidationError("assistant remind_at must be in the future")
    if "delivery" in payload and payload["delivery"] not in DELIVERIES:
        raise ValidationError("assistant delivery must be PUSH, ALARM or PUSH_AND_ALARM")
    for field in ("wake_check", "raise_volume"):
        if field in payload and not isinstance(payload[field], bool):
            raise ValidationError(f"assistant {field} must be a boolean")
    if creating and (payload.get("wake_check") or payload.get("raise_volume")) and \
            payload.get("delivery", "PUSH") not in ("ALARM", "PUSH_AND_ALARM"):
        raise ValidationError("assistant wake_check/raise_volume need an alarm delivery")
    if payload.get("note") is not None and (not isinstance(payload["note"], str) or len(payload["note"]) > 2000):
        raise ValidationError("assistant note must be text up to 2000 characters")


def _validate_task_fields(payload: dict[str, Any], now: datetime) -> None:
    """Semantic checks of the task fields a model may propose (same rules as task.create)."""
    from student_execution_os.sync.commands import parse_cutoff, parse_instant

    if "actual_cutoff" in payload:
        if not isinstance(payload["actual_cutoff"], dict) or set(payload["actual_cutoff"]) - {"state", "at", "boundary", "precision"}:
            raise ValidationError("assistant actual_cutoff must be {state, at?, boundary?}")
        try:
            parse_cutoff(payload["actual_cutoff"])
        except ValueError as exc:
            raise ValidationError(f"assistant actual_cutoff is invalid: {exc}") from exc
    for field in ("target_at", "actionable_from", "remind_at"):
        if payload.get(field) is not None:
            if not isinstance(payload[field], str):
                raise ValidationError(f"assistant {field} must be an ISO-8601 instant")
            parse_instant(payload[field], field)
    if payload.get("remind_at") is not None and parse_instant(payload["remind_at"], "remind_at") <= now:
        raise ValidationError("assistant remind_at must be in the future")
    if "splittable" in payload and not isinstance(payload["splittable"], bool):
        raise ValidationError("assistant splittable must be a boolean")
    for field in ("min_chunk_minutes", "max_chunk_minutes"):
        if field in payload:
            _positive_minutes(payload[field], field, allow_none=True)
    low, high = payload.get("min_chunk_minutes"), payload.get("max_chunk_minutes")
    if low is not None and high is not None and low > high:
        raise ValidationError("assistant min_chunk_minutes must not exceed max_chunk_minutes")


class SQLiteAssistantService:
    def __init__(self, canonical: SQLiteCanonicalRepository, principal: AuthenticatedPrincipal,
                 provider: AssistantProvider | None = None,
                 reliability_policy: ReliabilityPolicy | None = None) -> None:
        self.canonical = canonical
        self.principal = principal
        self.provider = provider or DeterministicAssistantParser()
        self.reliability_policy = reliability_policy or ReliabilityPolicy()
        self.reliability_trace = ReliabilityTrace()

    def _context(self, client: dict[str, object]) -> dict[str, object]:
        """Server-owned context: the model only sees what the account already owns."""
        rows = self.canonical.connection.execute(
            "SELECT o.id,o.kind,o.title,o.version,o.lifecycle_status,t.actual_cutoff_at AS cutoff_at,e.starts_at FROM obligations o "
            "LEFT JOIN tasks t ON t.obligation_id=o.id LEFT JOIN events e ON e.obligation_id=o.id WHERE o.account_id=? "
            "AND (o.lifecycle_status IN ('ACTIVE','DRAFT') OR (o.lifecycle_status='COMPLETED' AND o.updated_at>=?)) "
            "ORDER BY o.updated_at DESC LIMIT 80",
            (self.principal.account_id, _iso(self.canonical.clock.now() - timedelta(days=14))),
        ).fetchall()
        reminder_rows = self.canonical.connection.execute(
            "SELECT id,title,version,status,remind_at FROM reminders WHERE account_id=? AND status IN ('SCHEDULED','FIRED') "
            "ORDER BY remind_at LIMIT 40", (self.principal.account_id,),
        ).fetchall()
        from student_execution_os.reminders import ReminderStore
        prefs = ReminderStore(self.canonical).prefs(self.principal.account_id)
        zone = prefs.timezone_name
        requested = client.get("timezone")
        if isinstance(requested, str) and 0 < len(requested) <= 64:
            try:
                ZoneInfo(requested)
                zone = requested  # the device's zone: "в 18:00" means 18:00 where the user is
            except (ZoneInfoNotFoundError, ValueError):
                pass
        context: dict[str, object] = {
            "now": _iso(self.canonical.clock.now()), "timezone": zone,
            "obligations": [{"id": row["id"], "kind": row["kind"], "title": row["title"], "version": int(row["version"]),
                             "status": row["lifecycle_status"],
                             **({"due": row["cutoff_at"]} if row["cutoff_at"] else {}),
                             **({"starts_at": row["starts_at"]} if row["starts_at"] else {})} for row in rows],
            "reminders": [{"id": row["id"], "kind": "REMINDER", "title": row["title"], "version": int(row["version"]),
                           "status": row["status"], "remind_at": row["remind_at"]} for row in reminder_rows],
        }
        if isinstance(client.get("locale"), str):
            context["locale"] = client["locale"][:16]
        if client.get("source") in {"TEXT", "VOICE"}:
            context["source"] = client["source"]
        previous_batch_id = client.get("previous_batch_id")
        if previous_batch_id is not None:
            context["assistant_session"] = self._previous_session(
                str(previous_batch_id), client.get("previous_edits"),
            )
        return context

    def _previous_session(self, batch_id: str, edits: object) -> dict[str, object]:
        row = self.canonical.connection.execute(
            "SELECT actions_json,expires_at FROM assistant_batches "
            "WHERE account_id=? AND principal_id=? AND id=?",
            (self.principal.account_id, self.principal.principal_id, batch_id),
        ).fetchone()
        if row is None:
            raise AuthorizationDenied("previous assistant batch is not scoped to this principal")
        if _dt(row["expires_at"]) <= self.canonical.clock.now():
            raise AuthorizationDenied("previous assistant batch expired")
        actions = json.loads(row["actions_json"])
        if not isinstance(actions, list) or len(actions) > 10:
            raise ValidationError("previous assistant batch is invalid")
        by_id = {action.get("id"): action for action in actions if isinstance(action, dict)}
        if edits is None:
            edits = {}
        if not isinstance(edits, dict) or not set(edits).issubset(by_id) \
                or not all(isinstance(value, dict) for value in edits.values()):
            raise ValidationError("previous_edits must map previous action ids to field objects")
        refined = [self._edited(action, edits.get(action.get("id"))) for action in actions]
        return {
            "previous_batch_id": batch_id,
            "previous_actions": [
                {
                    key: action[key]
                    for key in ("command", "payload", "expected_version", "unresolved_fields", "provenance", "resolution")
                    if key in action
                }
                for action in refined
            ],
        }

    def interpret(self, text: str, context: dict[str, object] | None = None, *,
                  degrade_invalid: bool = False) -> dict[str, object]:
        started = perf_counter()
        self.reliability_trace = ReliabilityTrace()
        self.provider_failure = None
        try:
            result = self._interpret(text, context, degrade_invalid=degrade_invalid)
        except (ProviderUnavailable, ValidationError) as exc:
            self._record_interpret_metrics(started, error=exc)
            raise
        self._record_interpret_metrics(started, result=result)
        return result

    def _interpret(self, text: str, context: dict[str, object] | None = None, *,
                   degrade_invalid: bool = False) -> dict[str, object]:
        """Interpret ``text`` into a stored, expiring preview batch.

        A provider outage (or a refused key, an unsupported model, a non-JSON answer)
        degrades to the local parser and says so in ``fallback_reason``. With
        ``degrade_invalid`` a *well-formed but invalid* proposal is handled the same way
        (reason ``INVALID_PROPOSAL``); without it the call fails. Either way nothing a
        model got wrong is stored or shown.
        """
        if not str(text or "").strip():
            raise ValidationError("assistant input text is required")
        if len(str(text)) > 4000:
            raise ValidationError("assistant input is longer than 4000 characters")
        server_context = self._context(context if isinstance(context, dict) else {})
        local = isinstance(self.provider, DeterministicAssistantParser)
        try:
            provider_name, message, actions, read_result = self._propose(self.provider, text, server_context)
        except ProviderUnavailable as exc:
            # Provider outage (or a rejected key) degrades to the local parser instead
            # of failing the user; the reason code tells the client why.
            self.provider_failure = exc
            provider_name, message, actions, read_result = self._local(text, server_context)
        except ValidationError as exc:
            if local or not degrade_invalid:
                raise
            self.provider_failure = ProviderUnavailable(str(exc), "INVALID_PROPOSAL")
            provider_name, message, actions, read_result = self._local(text, server_context)
        fallback = self.provider_failure is not None
        now = self.canonical.clock.now()
        if read_result is not None:
            return {
                "batch_id": None,
                "provider": provider_name,
                "fallback": fallback,
                "engine": "LOCAL" if local or fallback else "AI",
                "model": None if local or fallback else getattr(self.provider, "model", None),
                "fallback_reason": None if self.provider_failure is None else self.provider_failure.reason,
                "retry_after_seconds": getattr(self.provider_failure, "retry_after", None),
                "reliability": {
                    "attempts": self.reliability_trace.attempts,
                    "retries": self.reliability_trace.retries,
                    "repair_attempted": self.reliability_trace.repair_attempted,
                    "repair_succeeded": self.reliability_trace.repair_succeeded,
                    "retry_stop": self.reliability_trace.stop_reason,
                },
                "message": message,
                "actions": [],
                "read": read_result,
                "created_at": _iso(now),
                "expires_at": None,
                "mutated_canonical_state": False,
            }
        batch_id = str(uuid4())
        redacted = re.sub(r"\b[\w.+-]+@[\w.-]+\b", "[email]", text)[:2000]
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.canonical._tx() as conn:
            # Expired previews can no longer be applied; their text is deleted, not kept.
            conn.execute("DELETE FROM assistant_batches WHERE account_id=? AND expires_at<=?", (self.principal.account_id, _iso(now)))
            conn.execute(
                "INSERT INTO assistant_batches(id,account_id,principal_id,input_hash,provider,redacted_input,actions_json,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, self.principal.account_id, self.principal.principal_id, digest, provider_name,
                 redacted, json.dumps(actions, sort_keys=True), _iso(now), _iso(now + timedelta(minutes=30))),
            )
        return {"batch_id": batch_id, "provider": provider_name, "fallback": fallback,
                # Which interpreter produced this preview: the user must be able to see
                # whether a language model or the local parser read their words.
                "engine": "LOCAL" if local or fallback else "AI",
                "model": None if local or fallback else getattr(self.provider, "model", None),
                "fallback_reason": None if self.provider_failure is None else self.provider_failure.reason,
                # A rate-limited provider's Retry-After, so a client can say when to retry.
                "retry_after_seconds": getattr(self.provider_failure, "retry_after", None),
                "reliability": {
                    "attempts": self.reliability_trace.attempts,
                    "retries": self.reliability_trace.retries,
                    "repair_attempted": self.reliability_trace.repair_attempted,
                    "repair_succeeded": self.reliability_trace.repair_succeeded,
                    "retry_stop": self.reliability_trace.stop_reason,
                },
                "message": message, "actions": actions,
                "created_at": _iso(now), "expires_at": _iso(now + timedelta(minutes=30)), "mutated_canonical_state": False}

    def _record_interpret_metrics(
        self,
        started: float,
        *,
        result: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        metrics = SQLiteOperationalMetrics(self.canonical)
        provider = getattr(self.provider, "name", "unknown")
        reason = (
            self.provider_failure.reason if error is not None and self.provider_failure is not None
            else error.reason if isinstance(error, ProviderUnavailable)
            else sanitized_validation_feedback(error) if error is not None
            else result.get("fallback_reason") if result is not None
            else None
        )
        engine = str(result.get("engine")) if result is not None else "NONE"
        dimensions = {"engine": engine, "provider": provider, "reason": reason or "NONE"}
        metrics.record(
            "assistant_interpretation_count",
            account_id=self.principal.account_id,
            dimensions={**dimensions, "result": "ERROR" if error is not None else "OK"},
        )
        metrics.record(
            "assistant_interpretation_latency_ms",
            (perf_counter() - started) * 1000,
            account_id=self.principal.account_id,
            dimensions=dimensions,
        )
        if reason and reason != "NONE":
            metrics.record(
                "assistant_provider_failure_count",
                account_id=self.principal.account_id,
                dimensions={"provider": provider, "reason": reason},
            )
        if result is not None and result.get("fallback"):
            metrics.record(
                "assistant_local_fallback_count",
                account_id=self.principal.account_id,
                dimensions={"provider": provider, "reason": reason or "UNKNOWN"},
            )
        structured_reason = self.reliability_trace.repair_reason or (
            reason if reason in {"FORMAT", "INVALID_PROPOSAL", "JSON_SCHEMA", "ACTION_SCHEMA"} else None
        )
        if structured_reason:
            metrics.record(
                "assistant_structured_output_failure_count",
                account_id=self.principal.account_id,
                dimensions={"provider": provider, "reason": structured_reason},
            )
        if self.reliability_trace.retries:
            metrics.record(
                "assistant_provider_retry_count",
                self.reliability_trace.retries,
                account_id=self.principal.account_id,
                dimensions={"provider": provider},
            )
        if self.reliability_trace.format_downgrades:
            # The endpoint refused json_schema and the call was re-sent in JSON mode.
            metrics.record(
                "assistant_format_downgrade_count",
                self.reliability_trace.format_downgrades,
                account_id=self.principal.account_id,
                dimensions={"provider": provider},
            )
        if self.reliability_trace.stop_reason:
            # Why a failed provider call was not retried (UNKNOWN_OUTCOME, BUDGET_EXHAUSTED, ...).
            metrics.record(
                "assistant_retry_stop_count",
                account_id=self.principal.account_id,
                dimensions={"provider": provider, "decision": self.reliability_trace.stop_reason},
            )
        if self.reliability_trace.repair_attempted:
            metrics.record(
                "assistant_repair_attempt_count",
                account_id=self.principal.account_id,
                dimensions={"provider": provider, "succeeded": self.reliability_trace.repair_succeeded},
            )
        if result is not None:
            ambiguous = sum(
                1 for action in result.get("actions", [])
                if isinstance(action, dict) and _target_unresolved(action.get("unresolved_fields") or [])
            )
            if ambiguous:
                metrics.record(
                    "assistant_target_ambiguity_count",
                    ambiguous,
                    account_id=self.principal.account_id,
                    dimensions={"provider": provider},
                )

    def _local(self, text: str, context: dict[str, object]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None]:
        try:
            return self._propose(DeterministicAssistantParser(), text, context)
        except ValidationError:
            raise ValidationError("the language model is unavailable and the local parser did not understand the input") from None

    def _propose(self, provider: AssistantProvider, text: str,
                 context: dict[str, object]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None]:
        """Ask one provider and validate every action it proposes (nothing is stored)."""
        try:
            if isinstance(provider, DeterministicAssistantParser):
                # The local parser is not a provider call: no retry, no budget, no attempt.
                interpretation = provider.interpret(text, context)
            else:
                interpretation = self.reliability_policy.run(
                    lambda: provider.interpret(text, context), trace=self.reliability_trace,
                )
            return self._validate_interpretation(provider, interpretation, text, context)
        except (ProviderUnavailable, ValidationError) as exc:
            repair = getattr(provider, "repair", None)
            repairable = (
                isinstance(exc, ProviderUnavailable) and exc.reason == "FORMAT"
            ) or (isinstance(exc, ValidationError) and not isinstance(exc, ProviderUnavailable))
            if not repairable or not callable(repair):
                raise
            self.reliability_trace.repair_attempted = True
            feedback = sanitized_validation_feedback(exc)
            self.reliability_trace.repair_reason = feedback
            try:
                interpretation = self.reliability_policy.run(
                    lambda: repair(text, context, feedback), trace=self.reliability_trace,
                )
                result = self._validate_interpretation(provider, interpretation, text, context)
            except (ProviderUnavailable, ValidationError):
                raise exc
            self.reliability_trace.repair_succeeded = True
            return result

    def _validate_interpretation(self, provider: AssistantProvider, interpretation: object, text: str,
                                 context: dict[str, object]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any] | None]:
        if isinstance(interpretation, dict):
            raw_actions = interpretation.get("actions")
            raw_read = interpretation.get("read_query")
            assistant_message = str(interpretation.get("message") or "")[:2000]
        else:
            raw_actions = interpretation
            raw_read = None
            assistant_message = "I prepared a structured preview. Review it before applying."
        if not isinstance(raw_actions, list):
            raise ValidationError("assistant provider returned an invalid actions list")
        if len(raw_actions) > 10:
            raise ValidationError("assistant proposed too many actions")
        if any(isinstance(action, dict) and action.get("command") == AgentCommand.UNDO_LAST.value
               for action in raw_actions) and len(raw_actions) != 1:
            raise ValidationError("assistant undo must be the only proposed action")
        if raw_read is not None:
            if raw_actions:
                raise ValidationError("assistant response cannot mix read query and mutations")
            read_result = SQLiteAssistantReadService(
                self.canonical, self.principal.account_id,
            ).execute(raw_read)
            return provider.name, assistant_message, [], read_result
        raw_actions = self._reconcile_explicit_intent(text, context, raw_actions)
        raw_actions, preserved = self._preserve_previous_user_edits(raw_actions, context)
        action_ids, dependencies = self._action_dependencies(raw_actions)
        actions: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_actions):
            derived: tuple[str, ...] = ()
            if isinstance(raw, dict) and isinstance(raw.get("payload"), dict) and _RELATIVE in raw["payload"]:
                raw, derived = self._relative_from_model(raw, action_ids, dependencies[index], actions)
            raw, guard = self._guard_target(raw, text, context)
            clean = validate_proposal(raw, self.canonical, self.principal.account_id,
                                      timezone_name=str(context.get("timezone") or "UTC"))
            field_provenance = clean.pop("field_provenance")
            local = isinstance(provider, DeterministicAssistantParser)
            actions.append({
                "id": action_ids[index], **clean,
                "depends_on": dependencies[index],
                "provenance": {
                    "provider": provider.name,
                    "input": "user-authored-text",
                    "fields": {
                        field: "LOCAL_INFERRED" if local else value
                        for field, value in field_provenance.items()
                    },
                },
                # Trust boundaries are server-owned. A provider cannot downgrade a
                # destructive command merely by emitting a false flag.
                "requires_confirmation": clean["command"] in DESTRUCTIVE or clean["requires_confirmation"],
            })
            if guard is not None:
                # The server, not the model, decided this target is not unique.
                actions[-1]["target_guard"] = guard.reason
                actions[-1]["target_candidates"] = [c.payload() for c in guard.candidates]
            if derived:
                actions[-1]["provenance"]["fields"].update({field: "DERIVED" for field in derived})
                actions[-1]["resolution"] = self._relative_resolution(actions[-1], actions)
            blocked = self._source_owned_block(actions[-1])
            if blocked:
                actions[-1]["blocked"] = blocked
            actions[-1]["provenance"]["input"] = (
                "voice-transcript" if context.get("source") == "VOICE" else "user-authored-text"
            )
            actions[-1]["provenance"]["fields"].update(
                {field: "USER_EDIT" for field in preserved.get(index, set())}
            )
        self._attach_conflicts(actions)
        return provider.name, assistant_message, actions, None

    def _guard_target(self, raw: object, text: str, context: dict[str, object]):
        """Server authority over the model's target pick (agent.disambiguation).

        Returns the (possibly unresolved) raw action and the ambiguity verdict, if any.
        A pick outside the authorized context is rejected like an invented id.
        """
        if not isinstance(raw, dict) or raw.get("command") not in _TARGET_KINDS:
            return raw, None
        payload = raw.get("payload")
        unresolved = raw.get("unresolved_fields")
        if not isinstance(payload, dict) or not isinstance(unresolved, list) or _target_unresolved(unresolved):
            return raw, None
        chosen = payload.get("reminder_id") or payload.get("obligation_id")
        if not chosen or (payload.get("reminder_id") and payload.get("obligation_id")):
            return raw, None  # validate_proposal rejects these shapes
        destination = None
        for field in ("when", "until"):
            if isinstance(payload.get(field), str):
                try:
                    destination = _instant(payload[field], field)
                except ValidationError:
                    destination = None
                break
        try:
            zone = ZoneInfo(str(context.get("timezone") or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        verdict = judge(
            chosen_id=str(chosen), allowed_kinds=_TARGET_KINDS[raw["command"]], text=text,
            target_text=payload.get("target_text"), context=context,
            now=self.canonical.clock.now(), zone=zone, destination=destination,
        )
        if verdict.decision == "OUT_OF_SCOPE":
            raise ValidationError("assistant proposal references an item outside its authorized context")
        if verdict.decision == "CONTINUE":
            return raw, None
        unresolved_payload = {k: v for k, v in payload.items() if k not in {"obligation_id", "reminder_id"}}
        provenance = raw.get("field_provenance")
        guarded = {
            **raw,
            "payload": unresolved_payload,
            "unresolved_fields": [*unresolved, "target"],
            "expected_version": None,
        }
        if isinstance(provenance, dict):
            guarded["field_provenance"] = {k: v for k, v in provenance.items() if k in unresolved_payload}
        return guarded, verdict

    @staticmethod
    def _action_dependencies(raw_actions: list[object]) -> tuple[list[str], list[list[str]]]:
        action_ids = [str(uuid4()) for _ in raw_actions]
        references: dict[str, int] = {}
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, dict):
                continue
            reference = raw.get("client_ref")
            if reference is None:
                continue
            if not isinstance(reference, str) or not reference or len(reference) > 64 or reference in references:
                raise ValidationError("assistant action client_ref must be unique short text")
            references[reference] = index
        dependencies: list[list[str]] = []
        for index, raw in enumerate(raw_actions):
            values = raw.get("depends_on", []) if isinstance(raw, dict) else []
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values) \
                    or len(set(values)) != len(values):
                raise ValidationError("assistant action depends_on must be unique client refs")
            resolved = []
            for value in values:
                dependency_index = references.get(value)
                if dependency_index is None or dependency_index >= index:
                    raise ValidationError("assistant action dependency must reference an earlier action")
                resolved.append(action_ids[dependency_index])
            dependencies.append(resolved)
        return action_ids, dependencies

    def _relative_from_model(
        self, raw: dict[str, Any], action_ids: list[str], dependency_ids: list[str], earlier: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Bind a model's ``relative_to`` (a client_ref) to the earlier action and derive the time.

        The reference must be one of the action's own dependencies, so a dependent
        time can never outlive (or be applied without) the action it is measured from.
        """
        command = raw.get("command")
        if command not in _RELATIVE_FIELDS:
            raise ValidationError(f"assistant {command} cannot take a time relative to another action")
        relative = parse_relative_to(raw["payload"][_RELATIVE])
        declared = raw.get("depends_on") or []
        if relative.action not in declared:
            raise ValidationError("assistant relative_to must reference an action listed in depends_on")
        referenced_id = dependency_ids[declared.index(relative.action)]
        referenced = next(action for action in earlier if action["id"] == referenced_id)
        fields = _RELATIVE_FIELDS[command]
        payload = {**raw["payload"], _RELATIVE: relative.as_payload(referenced_id)}
        unresolved = list(raw.get("unresolved_fields") or [])
        interval = self._action_interval(referenced)
        if interval is None:
            # The earlier action still needs the user (e.g. which meeting?); the
            # dependent time is derived once that is answered, at apply.
            for field in fields:
                payload.pop(field, None)
                if field not in unresolved:
                    unresolved.append(field)
        else:
            payload.update(self._relative_values(str(command), payload, relative, interval))
        provenance = raw.get("field_provenance")
        provenance = {key: value for key, value in provenance.items() if key not in fields} \
            if isinstance(provenance, dict) else provenance
        return {**raw, "payload": payload, "unresolved_fields": unresolved, "field_provenance": provenance}, fields

    def _event_result_interval(self, action: dict[str, Any]) -> tuple[datetime, datetime] | None:
        """The interval an action gives an event (None for anything that does not move one)."""
        command, payload = action["command"], action["payload"]
        if command == AgentCommand.UPDATE_EVENT.value and not {"starts_at", "ends_at"} & set(payload):
            return None
        if command not in (AgentCommand.CREATE_EVENT.value, AgentCommand.RESCHEDULE.value,
                           AgentCommand.UPDATE_EVENT.value):
            return None
        if command == AgentCommand.RESCHEDULE.value:
            target = target_of(self.canonical, self.principal.account_id, payload)
            if target is None or target[0] != "EVENT":
                return None
        try:
            return self._action_interval(action)
        except ValidationError:
            return None

    def _attach_conflicts(self, actions: list[dict[str, Any]]) -> None:
        """Warn, before confirmation, where a new event time overlaps something fixed.

        Checked against other active events, class-series occurrences, protected time
        (UNAVAILABLE / FIXED_PERSONAL_BLOCK constraints) and the other items of the
        same plan. Nothing is moved to make room: the user sees the overlap and decides.
        Derived plan blocks are not conflicts (the planner re-derives them).
        """
        from student_execution_os.recurrence import SQLiteRecurrenceRepository
        account = self.principal.account_id
        planned: dict[str, tuple[datetime, datetime]] = {}
        titles: dict[str, str | None] = {}
        targets: dict[str, str | None] = {}
        for action in actions:
            interval = self._event_result_interval(action)
            if interval is None:
                continue
            planned[action["id"]] = interval
            target = target_of(self.canonical, account, action["payload"])
            targets[action["id"]] = target[1] if target else None
            titles[action["id"]] = action["payload"].get("title") or (
                self.canonical.get_event(account, target[1]).obligation.title if target else None)
        moving = {target for target in targets.values() if target}  # compared at their new time instead
        for action_id, (starts, ends) in planned.items():
            found: list[dict[str, Any]] = []
            for row in self.canonical.connection.execute(
                "SELECT o.id,o.title,e.starts_at,e.ends_at FROM obligations o JOIN events e ON e.obligation_id=o.id "
                "WHERE o.account_id=? AND o.lifecycle_status='ACTIVE' AND e.starts_at<? AND e.ends_at>? "
                "ORDER BY e.starts_at LIMIT 6", (account, _iso(ends), _iso(starts)),
            ):
                if row["id"] not in moving:
                    found.append({"kind": "EVENT", "title": row["title"], "starts_at": row["starts_at"],
                                  "ends_at": row["ends_at"]})
            for event in SQLiteRecurrenceRepository(self.canonical).expand_as_events(
                    account_id=account, horizon_start=starts, horizon_end=ends):
                if event.interval.starts_at < ends and event.interval.ends_at > starts:
                    found.append({"kind": "CLASS", "title": event.obligation.title,
                                  "starts_at": _iso(event.interval.starts_at), "ends_at": _iso(event.interval.ends_at)})
            for row in self.canonical.connection.execute(
                "SELECT starts_at,ends_at,reason FROM user_time_constraints WHERE account_id=? "
                "AND type IN ('UNAVAILABLE','FIXED_PERSONAL_BLOCK') AND starts_at<? AND ends_at>? "
                "ORDER BY starts_at LIMIT 3", (account, _iso(ends), _iso(starts)),
            ):
                found.append({"kind": "PROTECTED_TIME", "title": row["reason"], "starts_at": row["starts_at"],
                              "ends_at": row["ends_at"]})
            for other_id, (other_starts, other_ends) in planned.items():
                if other_id != action_id and other_starts < ends and other_ends > starts:
                    found.append({"kind": "PLAN", "title": titles[other_id],
                                  "starts_at": _iso(other_starts), "ends_at": _iso(other_ends)})
            if found:
                next(action for action in actions if action["id"] == action_id)["conflicts"] = found[:6]

    # Time and wording of an imported calendar event belong to its source; only the
    # personal reminder lead (and cancelling it for oneself) is the user's —
    # sync/handlers/events.py enforces the same at apply.
    _SOURCE_OWNED_COMMANDS = {AgentCommand.RESCHEDULE.value, AgentCommand.UPDATE_EVENT.value}

    def _source_owned_block(self, action: dict[str, Any]) -> dict[str, str] | None:
        """Why an action can never apply to a source-owned event, shown in the preview."""
        if action["command"] not in self._SOURCE_OWNED_COMMANDS:
            return None
        payload = action["payload"]
        if action["command"] == AgentCommand.UPDATE_EVENT.value \
                and not set(payload) - _TARGET - {"remind_before_minutes"}:
            return None
        target = target_of(self.canonical, self.principal.account_id, payload)
        if target is None or target[0] != "EVENT":
            return None
        imported = self.canonical.connection.execute(
            "SELECT 1 FROM external_identities WHERE account_id=? AND local_kind='EVENT' AND local_id=? "
            "AND external_recurrence_id=''", (self.principal.account_id, target[1]),
        ).fetchone()
        if imported is None:
            return None
        return {"code": "IMPORTED_EVENT_SOURCE_OWNED",
                "message": "this event comes from an imported calendar; its time changes only at the source"}

    @staticmethod
    def _relative_values(command: str, payload: dict[str, Any], relative: RelativeToAction,
                         interval: tuple[datetime, datetime]) -> dict[str, Any]:
        moment = relative.moment(*interval)
        if command == AgentCommand.CREATE_EVENT.value:
            duration = payload.get("duration_minutes")
            if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
                duration = 60  # the same default as an event given only a start
            return {"starts_at": _iso(moment), "ends_at": _iso(moment + timedelta(minutes=duration)),
                    "duration_minutes": duration}
        if command == AgentCommand.CREATE_TASK.value:
            return {"actionable_from": _iso(moment)}
        return {"remind_at": _iso(moment)}

    def _action_interval(self, action: dict[str, Any]) -> tuple[datetime, datetime] | None:
        """The interval an action will leave its item in, exactly as apply computes it.

        None while the action's own target or time is still unresolved.
        """
        command, payload = action["command"], action["payload"]
        if command == AgentCommand.CREATE_EVENT.value:
            if payload.get("starts_at") and payload.get("ends_at"):
                return _dt(str(payload["starts_at"])), _dt(str(payload["ends_at"]))
            return None
        if command == AgentCommand.CREATE_REMINDER.value:
            return (_dt(str(payload["remind_at"])),) * 2 if payload.get("remind_at") else None
        if command not in (AgentCommand.RESCHEDULE.value, AgentCommand.UPDATE_EVENT.value):
            raise ValidationError("assistant relative_to must reference an action that sets a time")
        target = target_of(self.canonical, self.principal.account_id, payload)
        if target is None:
            return None
        kind, entity_id, _version = target
        if kind == "EVENT":
            event = self.canonical.get_event(self.principal.account_id, entity_id).interval
            length = event.ends_at - event.starts_at
            if command == AgentCommand.UPDATE_EVENT.value:
                starts = _dt(str(payload["starts_at"])) if payload.get("starts_at") else event.starts_at
                if payload.get("ends_at"):
                    return starts, _dt(str(payload["ends_at"]))
                return starts, (starts + length if payload.get("starts_at") else event.ends_at)
            if not payload.get("when"):
                return None
            from student_execution_os.reminders import ReminderStore
            zone = ZoneInfo(ReminderStore(self.canonical).prefs(self.principal.account_id).timezone_name)
            _operation, body = reschedule_change("EVENT", {"starts_at": _iso(event.starts_at)}, _dt(str(payload["when"])),
                                                 bool(payload.get("keep_time")), zone)
            starts = _dt(body["starts_at"])
            return starts, starts + length
        if kind == "REMINDER" and command == AgentCommand.RESCHEDULE.value and payload.get("when"):
            current = _target_moment(self.canonical, self.principal.account_id, kind, entity_id)
            from student_execution_os.reminders import ReminderStore
            zone = ZoneInfo(ReminderStore(self.canonical).prefs(self.principal.account_id).timezone_name)
            _operation, body = reschedule_change("REMINDER", {"remind_at": _iso(current)}, _dt(str(payload["when"])),
                                                 bool(payload.get("keep_time")), zone)
            return (_dt(body["remind_at"]),) * 2
        raise ValidationError("assistant relative_to must reference an event or reminder time")

    @staticmethod
    def _relative_resolution(action: dict[str, Any], earlier: list[dict[str, Any]]) -> dict[str, Any]:
        relative = parse_relative_to(action["payload"][_RELATIVE])
        referenced = next(item for item in earlier if item["id"] == relative.action)
        first = _RELATIVE_FIELDS[action["command"]][0]
        return {
            "reason": "RELATIVE_TO_ACTION", **relative.as_payload(),
            "when": action["payload"].get(first),
            # «после неё» is only as exact as the time it is measured from.
            "precision": (referenced.get("resolution") or {}).get("precision", TemporalPrecision.EXACT.value),
        }

    def _rebase_relative(self, action: dict[str, Any], earlier: list[dict[str, Any]]) -> dict[str, Any]:
        """Re-derive a relative time from the earlier action as it will actually be applied.

        A correction to the earlier action («нет, лучше на 10:30») moves the
        dependent time with it; an explicit user edit of the dependent time itself
        already removed ``relative_to`` in ``_edited``.
        """
        if action["payload"].get(_RELATIVE) is None:
            return action
        relative = parse_relative_to(action["payload"][_RELATIVE])
        referenced = next((item for item in earlier if item["id"] == relative.action), None)
        if referenced is None:
            raise ValidationError("selected Assistant actions must include every dependency")
        interval = self._action_interval(referenced)
        if interval is None:
            raise ValidationError("unresolved proposal fields must be refined before apply")
        fields = _RELATIVE_FIELDS[action["command"]]
        raw = {key: action[key] for key in _ACTION_REQUIRED}
        raw["payload"] = {**action["payload"],
                          **self._relative_values(action["command"], action["payload"], relative, interval)}
        raw["unresolved_fields"] = [field for field in action["unresolved_fields"] if field not in fields]
        clean = validate_proposal(raw, self.canonical, self.principal.account_id)
        clean.pop("field_provenance", None)
        rebased = {**action, **clean}
        rebased["resolution"] = self._relative_resolution(rebased, earlier)
        return rebased

    @staticmethod
    def _preserve_previous_user_edits(
        raw_actions: list[object], context: dict[str, object],
    ) -> tuple[list[object], dict[int, set[str]]]:
        session = context.get("assistant_session")
        previous = session.get("previous_actions") if isinstance(session, dict) else None
        if not isinstance(previous, list):
            return raw_actions, {}
        preserved: dict[int, set[str]] = {}
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
                continue
            candidates = []
            for prior in previous:
                if not isinstance(prior, dict) or not isinstance(prior.get("payload"), dict):
                    continue
                prior_payload = prior["payload"]
                same_target = any(
                    raw["payload"].get(field) is not None
                    and raw["payload"].get(field) == prior_payload.get(field)
                    for field in ("obligation_id", "reminder_id")
                )
                same_single_create = (
                    len(raw_actions) == len(previous) == 1
                    and str(raw.get("command", "")).startswith("CREATE_")
                    and raw.get("command") == prior.get("command")
                )
                if same_target or same_single_create:
                    candidates.append(prior)
            if len(candidates) != 1:
                continue
            prior = candidates[0]
            fields = (prior.get("provenance") or {}).get("fields")
            if not isinstance(fields, dict):
                continue
            incoming_provenance = raw.get("field_provenance")
            if not isinstance(incoming_provenance, dict):
                incoming_provenance = {}
                raw["field_provenance"] = incoming_provenance
            for field, source in fields.items():
                if source != "USER_EDIT" or incoming_provenance.get(field) == "MODEL_EXPLICIT":
                    continue
                if field in prior["payload"]:
                    raw["payload"][field] = prior["payload"][field]
                    incoming_provenance[field] = "MODEL_INFERRED"
                    preserved.setdefault(index, set()).add(field)
        return raw_actions, preserved

    @staticmethod
    def _reconcile_explicit_intent(text: str, context: dict[str, object],
                                   raw_actions: list[object]) -> list[object]:
        """Keep explicitly stated alarm semantics as a floor under the model's proposal.

        The language model enriches a proposal (title, time, note); it is not the
        authority to turn «поставь будильник» into a push, to drop «разбуди меня»
        wake semantics by omitting a field, or to capture an alarm as a task.
        ``parse_task`` is the single owner of RU/EN intent detection (the client runs
        the same rules in nlparse.js). Only creation is reconciled: a command about an
        existing item («отмени будильник») is not a new alarm request.
        """
        now = _dt(str(context.get("now"))) if context.get("now") else None
        parsed = parse_task(text, now=now or datetime.now(timezone.utc),
                            timezone_name=str(context.get("timezone") or "UTC"))
        floor = str(parsed.get("delivery") or "")
        if parsed.get("kind") != "REMINDER" or floor not in ("ALARM", "PUSH_AND_ALARM"):
            return raw_actions
        proposed = raw_actions[0] if len(raw_actions) == 1 else None
        if not isinstance(proposed, dict) or proposed.get("command") not in _CREATES:
            return raw_actions  # several items, or a command about an existing one
        local = _reminder_payload(parsed)
        if proposed.get("command") != AgentCommand.CREATE_REMINDER.value:
            # An alarm captured as a task/event is a semantic downgrade.
            return [{**proposed, "command": AgentCommand.CREATE_REMINDER.value, "payload": local,
                     "unresolved_fields": [], "expected_version": None}]
        incoming = proposed.get("payload")
        if not isinstance(incoming, dict):
            return raw_actions
        # Omitted fields keep the local reading; supplied ones are enrichment...
        payload = {**local, **incoming}
        # ...except that delivery may only keep or strengthen the alarm part.
        if floor == "PUSH_AND_ALARM" or payload.get("delivery") not in ("ALARM", "PUSH_AND_ALARM"):
            payload["delivery"] = floor
        if local.get("wake_check"):
            payload["wake_check"] = True
        return [{**proposed, "payload": payload}]

    def apply(self, payload: dict[str, object]) -> dict[str, object]:
        batch_id = str(payload.get("batch_id", ""))
        key = str(payload.get("idempotency_key", ""))
        selected = payload.get("action_ids")
        confirmed = set(payload.get("confirmed_action_ids") or [])
        edits = payload.get("edits") or {}
        if not batch_id or not key or not isinstance(selected, list) or not selected:
            raise ValidationError("batch_id, non-empty action_ids and idempotency_key are required")
        if not isinstance(edits, dict) or not all(isinstance(value, dict) for value in edits.values()):
            raise ValidationError("edits must map action ids to field objects")
        request = {"batch_id": batch_id, "action_ids": selected, "confirmed": sorted(confirmed)}
        if edits:
            request["edits"] = edits
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        replay = self.canonical.connection.execute(
            "SELECT request_hash,result_json FROM assistant_apply_records WHERE account_id=? AND principal_id=? AND idempotency_key=?",
            (self.principal.account_id, self.principal.principal_id, key),
        ).fetchone()
        if replay:
            if replay["request_hash"] != request_hash:
                raise IdempotencyConflict("assistant idempotency key was reused for different actions")
            result = json.loads(replay["result_json"])
            result["replayed"] = True
            return result
        row = self.canonical.connection.execute(
            "SELECT * FROM assistant_batches WHERE account_id=? AND principal_id=? AND id=?",
            (self.principal.account_id, self.principal.principal_id, batch_id),
        ).fetchone()
        if row is None:
            raise AuthorizationDenied("assistant batch is not scoped to this principal")
        if _dt(row["expires_at"]) <= self.canonical.clock.now():
            raise AuthorizationDenied("assistant batch expired")
        by_id = {item["id"]: item for item in json.loads(row["actions_json"])}
        if len(set(selected)) != len(selected) or not set(selected).issubset(by_id):
            raise ValidationError("selected action id is not in the preview batch")
        if not set(edits).issubset(selected):
            raise ValidationError("edits name an action that is not being applied")
        selected_set = set(selected)
        if any(not set(by_id[action_id].get("depends_on") or []).issubset(selected_set) for action_id in selected):
            raise ValidationError("selected Assistant actions must include every dependency")
        actions: list[dict[str, Any]] = []
        for stored in json.loads(row["actions_json"]):
            if stored["id"] in selected_set:
                # Declared order: an earlier action is final (with the user's edits)
                # before a time that depends on it is derived.
                actions.append(self._rebase_relative(self._edited(stored, edits.get(stored["id"])), actions))
        if any(action["unresolved_fields"] for action in actions):
            raise ValidationError("unresolved proposal fields must be refined before apply")
        if any(action["requires_confirmation"] and action["id"] not in confirmed for action in actions):
            raise AuthorizationDenied("destructive or ambiguous action requires explicit confirmation")
        for action in actions:
            blocked = self._source_owned_block(action)
            if blocked:
                raise ValidationError(blocked["message"])
        for action in actions:
            if action["command"] not in _TARGET_KINDS:
                continue
            target = target_of(self.canonical, self.principal.account_id, action["payload"])
            if target is None:
                raise ValidationError("the item this action is about no longer exists")
            if action["expected_version"] is None or target[2] != int(action["expected_version"]):
                raise VersionConflict("assistant proposal expected version is stale or missing")
        with self.canonical._tx() as conn:
            results = []
            for sequence, action in enumerate(actions):
                inverse = self._inverse(action)
                action_result = self._execute(action)
                results.append(action_result)
                if inverse is None:
                    inverse = self._creation_inverse(action, action_result)
                if inverse is not None and action_result.get("outcome") == "APPLIED" \
                        and isinstance(action_result.get("version"), int):
                    conn.execute(
                        "INSERT INTO assistant_action_history("
                        "account_id,principal_id,apply_idempotency_key,action_id,sequence_index,command,"
                        "entity_id,committed_version,inverse_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            self.principal.account_id,
                            self.principal.principal_id,
                            key,
                            action["id"],
                            sequence,
                            action["command"],
                            action_result["entity_id"],
                            action_result["version"],
                            json.dumps(inverse, sort_keys=True),
                            _iso(self.canonical.clock.now()),
                        ),
                    )
            result = {"batch_id": batch_id, "results": results, "replayed": False}
            conn.execute(
                "INSERT INTO assistant_apply_records(account_id,principal_id,idempotency_key,request_hash,result_json,created_at) VALUES (?,?,?,?,?,?)",
                (self.principal.account_id, self.principal.principal_id, key, request_hash,
                 json.dumps(result, sort_keys=True), _iso(self.canonical.clock.now())),
            )
        if edits:
            SQLiteOperationalMetrics(self.canonical).record(
                "assistant_user_correction_count",
                account_id=self.principal.account_id,
                dimensions={
                    "provider": str(row["provider"]),
                    "action_count": len(edits),
                    "field_count": sum(len(value) for value in edits.values()),
                },
            )
        return result

    def undo(self, payload: dict[str, object]) -> dict[str, object]:
        """The [Отменить] button: UNDO_LAST without asking a model to read «отмени».

        It is stored as a one-action batch and goes through ``apply``, so the same
        idempotency record, history bookkeeping and version checks apply; the batch id
        is derived from the key, so a retried request replays instead of undoing twice.
        """
        key = str(payload.get("idempotency_key", ""))
        if not key or len(key) > 200:
            raise ValidationError("idempotency_key is required")
        target = payload.get("apply_idempotency_key")
        if target is not None and (not isinstance(target, str) or not 0 < len(target) <= 200):
            raise ValidationError("apply_idempotency_key must be the key of an Assistant apply")
        scope = f"{self.principal.account_id}\0{self.principal.principal_id}\0{key}"
        batch_id = f"undo-{hashlib.sha256(scope.encode()).hexdigest()[:32]}"
        # Server-written (never model input): which apply the button belongs to.
        action = {"id": f"{batch_id}-0", "command": AgentCommand.UNDO_LAST.value,
                  "payload": {} if target is None else {"apply_idempotency_key": target}, "confidence": 1.0,
                  "unresolved_fields": [], "expected_version": None, "requires_confirmation": False, "depends_on": [],
                  "provenance": {"provider": "user-interface", "input": "undo-button", "fields": {}}}
        now = self.canonical.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO assistant_batches(id,account_id,principal_id,input_hash,provider,redacted_input,actions_json,"
                "created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (batch_id, self.principal.account_id, self.principal.principal_id,
                 hashlib.sha256(b"undo").hexdigest(), "user-interface", "", json.dumps([action], sort_keys=True),
                 _iso(now), _iso(now + timedelta(minutes=30))),
            )
        return self.apply({"batch_id": batch_id, "action_ids": [action["id"]], "idempotency_key": key})

    def _edited(self, action: dict[str, Any], edit: dict[str, Any] | None) -> dict[str, Any]:
        """Apply the user's answers/corrections from the preview card, then re-validate.

        Setting a field in ``edits`` resolves it, including an explicit "don't know"
        (``null`` effort → a draft; ``{"state": "UNKNOWN"}`` deadline).
        """
        if not edit:
            return action
        edit = dict(edit)
        raw = {key: action[key] for key in _ACTION_REQUIRED}
        if "expected_version" in edit:
            raw["expected_version"] = edit.pop("expected_version")
        payload = {**action["payload"], **edit}
        if "when" in edit and "temporal_transform" not in edit:
            # «нет, лучше на 10:30»: the user's exact time replaces the model's
            # (possibly approximate) relative transform instead of being recomputed away.
            payload.pop("temporal_transform", None)
        elif payload.get("temporal_transform") is not None:
            payload.pop("when", None)  # server-derived; recompute after the user's edit
        derived = set(_RELATIVE_FIELDS.get(action["command"], ()))
        if payload.get(_RELATIVE) is not None:
            if derived & edit.keys():
                payload.pop(_RELATIVE)  # an explicitly edited time is no longer "after it"
            elif action["command"] == AgentCommand.CREATE_EVENT.value and payload.get("starts_at") \
                    and isinstance(edit.get("duration_minutes"), int) and not isinstance(edit["duration_minutes"], bool):
                payload["ends_at"] = _iso(_dt(str(payload["starts_at"])) + timedelta(minutes=edit["duration_minutes"]))
        picked = "obligation_id" in edit or "reminder_id" in edit
        if picked:
            # The user chose which item the command is about.
            payload.pop("target_text", None)
            if "reminder_id" in edit:
                payload.pop("obligation_id", None)
        if (action["command"] == AgentCommand.CREATE_EVENT.value
                and ({"starts_at", "ends_at"} & edit.keys())
                and "duration_minutes" not in edit):
            # A reviewed time edit changes the interval. Re-derive the semantic
            # duration instead of letting the provider's old derived value veto it.
            payload.pop("duration_minutes", None)
        raw["payload"] = payload
        raw["unresolved_fields"] = [field for field in action["unresolved_fields"] if field not in edit
                                    and not (picked and field in {"target", "obligation_id", "reminder_id", "expected_version"})]
        clean = validate_proposal(
            raw, self.canonical, self.principal.account_id,
            timezone_name=str((action.get("resolution") or {}).get("timezone") or "") or None,
        )
        clean.pop("field_provenance", None)
        provenance = dict(action.get("provenance") or {})
        fields = {field: source for field, source in (provenance.get("fields") or {}).items() if field in clean["payload"]}
        fields.update({field: "USER_EDIT" for field in edit if field != "expected_version"})
        provenance["fields"] = fields
        return {**action, **clean, "provenance": provenance,
                "requires_confirmation": action["requires_confirmation"]}

    def _execute(self, action: dict[str, object]) -> dict[str, object]:
        command = AgentCommand(action["command"])
        data = action["payload"]
        common = {"account_id": self.principal.account_id, "actor": ActorCategory.USER_VIA_LLM}
        if command is AgentCommand.CREATE_TASK:
            # The same mapping as a task.create sync operation, so every proposal field
            # (deadline, target, start, reminder, chunking …) is stored — never dropped.
            from student_execution_os.sync.commands import Commands
            task_id = f"task-{uuid4()}"
            outcome = Commands(self.canonical, account_id=self.principal.account_id, actor=ActorCategory.USER_VIA_LLM,
                               now=self.canonical.clock.now()).tasks.task_create(
                task_id, {key: value for key, value in data.items() if key != _RELATIVE})
            return {"action_id": action["id"], "entity_id": task_id, "operation": "task.create", "outcome": outcome.status,
                    "version": outcome.entity["version"], "status": outcome.entity["status"], "entity": outcome.entity}
        if command is AgentCommand.REFINE_TASK:
            entity = str(data["obligation_id"])
            task = self.canonical.update_task(**common, obligation_id=entity, expected_version=int(action["expected_version"]),
                estimated_total_effort_minutes=int(data["estimated_total_effort_minutes"]), activate=bool(data.get("activate", False)))
            return {"action_id": action["id"], "entity_id": entity, "version": task.obligation.version, "status": task.obligation.lifecycle_status.value}
        if command is AgentCommand.UNDO_LAST:
            return {"action_id": action["id"], **self._undo_latest(data.get("apply_idempotency_key"))}
        # Everything else runs through the same command handlers as the offline sync
        # queue, so an Assistant action and a button press mean exactly the same thing.
        from student_execution_os.sync.commands import APPLIED, NOOP, Commands
        commands = Commands(self.canonical, account_id=self.principal.account_id, actor=ActorCategory.USER_VIA_LLM,
                            now=self.canonical.clock.now())
        op_type, entity, body = self._operation(command, data, commands)
        outcome = commands.run(op_type, entity, body)
        if outcome.status not in (APPLIED, NOOP):
            raise ValidationError(outcome.message or outcome.code or f"{op_type} was not applied")
        result = {"action_id": action["id"], "entity_id": entity, "operation": op_type, "outcome": outcome.status,
                  "entity": outcome.entity}
        if isinstance(outcome.entity, dict):
            result.update(version=outcome.entity.get("version"), status=outcome.entity.get("status"))
        return result

    def _operation(self, command: AgentCommand, data: dict[str, Any], commands) -> tuple[str, str, dict[str, Any]]:
        """The sync operation (type, entity id, payload) an action stands for."""
        target = target_of(self.canonical, self.principal.account_id, data)
        kind, entity = (target[0], target[1]) if target else ("", "")
        fields = {key: value for key, value in data.items() if key not in _TARGET and key != _RELATIVE}
        if command is AgentCommand.CREATE_NOTE:
            return "note.create", f"note-{uuid4()}", {**fields, "source_kind": "CAPTURE"}
        if command is AgentCommand.CREATE_EVENT:
            # One event.create owner for manual, offline and Assistant creation, so a
            # requested reminder lead (remind_before_minutes) is stored the same way.
            fields.pop("duration_minutes", None)  # derived semantic field, not an event.create input
            return "event.create", f"event-{uuid4()}", fields
        if command is AgentCommand.CREATE_REMINDER:
            return "reminder.create", f"reminder-{uuid4()}", fields | ({"obligation_id": data["obligation_id"]} if data.get("obligation_id") else {})
        if command is AgentCommand.CREATE_TIME_CONSTRAINT:
            return "constraint.create", f"constraint-{uuid4()}", fields
        if command is AgentCommand.CREATE_PLANNING_PREFERENCE:
            return "preference.create", f"preference-{uuid4()}", fields
        if command is AgentCommand.UPDATE_TASK:
            return "task.update", entity, fields
        if command is AgentCommand.UPDATE_EVENT:
            return "event.update", entity, fields
        if command is AgentCommand.UPDATE_REMINDER:
            return "reminder.update", entity, fields
        if command is AgentCommand.LOG_PROGRESS:
            return "task.progress", entity, {key: fields[key] for key in ("minutes", "count") if fields.get(key) is not None}
        if command is AgentCommand.SNOOZE:
            return "reminder.snooze", entity, {"until": fields["until"]}
        if command is AgentCommand.ARCHIVE_OBLIGATION:
            return "task.archive", entity, {}
        if command is AgentCommand.COMPLETE_OBLIGATION:
            return ("reminder.done" if kind == "REMINDER" else "task.complete"), entity, {}
        if command is AgentCommand.CANCEL_OBLIGATION:
            return {"REMINDER": "reminder.cancel", "EVENT": "event.cancel"}.get(kind, "task.cancel"), entity, {}
        if command is AgentCommand.RESCHEDULE:
            from student_execution_os.reminders import ReminderStore
            zone = ZoneInfo(ReminderStore(self.canonical).prefs(self.principal.account_id).timezone_name)
            current = commands.current_entity(kind, entity)
            op_type, body = reschedule_change(kind, current, _dt(str(fields["when"])), bool(fields.get("keep_time")), zone)
            return op_type, entity, body
        raise ValidationError(f"unsupported assistant command {command.value}")

    def _inverse(self, action: dict[str, Any]) -> dict[str, Any] | None:
        command = AgentCommand(action["command"])
        data = action["payload"]
        target = target_of(self.canonical, self.principal.account_id, data)
        if target is None:
            return None
        kind, entity_id, _version = target
        requested = {key for key in data if key not in _TARGET}
        if command in {AgentCommand.RESCHEDULE, AgentCommand.SNOOZE}:
            if kind == "EVENT":
                event = self.canonical.get_event(self.principal.account_id, entity_id)
                return {"operation": "event.update", "payload": {"starts_at": _iso(event.interval.starts_at)}}
            if kind == "REMINDER":
                row = self.canonical.connection.execute(
                    "SELECT remind_at FROM reminders WHERE account_id=? AND id=?",
                    (self.principal.account_id, entity_id),
                ).fetchone()
                return {"operation": "reminder.update", "payload": {"remind_at": row["remind_at"]}}
            row = self.canonical.connection.execute(
                "SELECT cutoff_state,actual_cutoff_at,cutoff_boundary,actionable_from FROM tasks "
                "WHERE obligation_id=?", (entity_id,),
            ).fetchone()
            if row["cutoff_state"] == "KNOWN":
                payload = {"actual_cutoff": {
                    "state": "KNOWN", "at": row["actual_cutoff_at"],
                    "boundary": row["cutoff_boundary"] or "INCLUSIVE",
                }}
            else:
                payload = {"actionable_from": row["actionable_from"]}
            return {"operation": "task.update", "payload": payload}
        if command is AgentCommand.UPDATE_EVENT:
            row = self.canonical.connection.execute(
                "SELECT o.title,o.description,e.starts_at,e.ends_at,e.attendance_policy FROM obligations o "
                "JOIN events e ON e.obligation_id=o.id WHERE o.account_id=? AND o.id=?",
                (self.principal.account_id, entity_id),
            ).fetchone()
            values = dict(row)
            if "remind_before_minutes" in requested:
                from student_execution_os.persistence import extras
                values["remind_before_minutes"] = extras.event_lead(
                    self.canonical, self.principal.account_id, entity_id,
                )
            return {"operation": "event.update", "payload": {
                field: values[field] for field in requested if field in values
            }}
        if command is AgentCommand.UPDATE_REMINDER:
            row = self.canonical.connection.execute(
                "SELECT title,note,remind_at,delivery,wake_check,raise_volume FROM reminders "
                "WHERE account_id=? AND id=?", (self.principal.account_id, entity_id),
            ).fetchone()
            values = dict(row)
            values.update(wake_check=bool(values["wake_check"]), raise_volume=bool(values["raise_volume"]))
            return {"operation": "reminder.update", "payload": {
                field: values[field] for field in requested if field in values
            }}
        if command is AgentCommand.UPDATE_TASK:
            row = self.canonical.connection.execute(
                "SELECT o.title,o.description,o.category,o.importance,t.estimated_total_effort_minutes,"
                "t.cutoff_state,t.actual_cutoff_at,t.cutoff_boundary,t.target_at,t.actionable_from "
                "FROM obligations o JOIN tasks t ON t.obligation_id=o.id "
                "WHERE o.account_id=? AND o.id=?", (self.principal.account_id, entity_id),
            ).fetchone()
            values = dict(row)
            values["actual_cutoff"] = (
                {"state": "KNOWN", "at": values["actual_cutoff_at"],
                 "boundary": values["cutoff_boundary"] or "INCLUSIVE"}
                if values["cutoff_state"] == "KNOWN" else {"state": values["cutoff_state"]}
            )
            if "remind_at" in requested:
                reminder = self.canonical.connection.execute(
                    "SELECT remind_at FROM reminder_states WHERE account_id=? AND obligation_id=?",
                    (self.principal.account_id, entity_id),
                ).fetchone()
                values["remind_at"] = None if reminder is None else reminder["remind_at"]
            return {"operation": "task.update", "payload": {
                field: values[field] for field in requested if field in values
            }}
        return None

    # Where each inverse's entity keeps its optimistic version.
    _VERSION_TABLES = {"reminder": ("reminders", "id"), "note": ("notes", "id"),
                       "constraint": ("user_time_constraints", "id"),
                       "preference": ("planning_preferences", "id")}

    def _current_version(self, operation: str, entity_id: str) -> int | None:
        table, key = self._VERSION_TABLES.get(operation.split(".", 1)[0], ("obligations", "id"))
        row = self.canonical.connection.execute(
            f"SELECT version FROM {table} WHERE account_id=? AND {key}=?",  # fixed identifiers above
            (self.principal.account_id, entity_id),
        ).fetchone()
        return None if row is None else int(row["version"])

    @staticmethod
    def _creation_inverse(action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
        """Undoing a creation deletes exactly that item, and only while it is unchanged."""
        operation = {
            AgentCommand.CREATE_TASK.value: "task.delete", AgentCommand.CREATE_EVENT.value: "event.delete",
            AgentCommand.CREATE_REMINDER.value: "reminder.delete", AgentCommand.CREATE_NOTE.value: "note.delete",
            AgentCommand.CREATE_TIME_CONSTRAINT.value: "constraint.delete",
            AgentCommand.CREATE_PLANNING_PREFERENCE.value: "preference.delete",
        }.get(action["command"])
        return None if operation is None else {"operation": operation, "payload": {}}

    def _undo_latest(self, expected_apply: object = None) -> dict[str, Any]:
        """Revert the most recent Assistant apply as a whole («отмени последнее», [Отменить]).

        Every reversible action of that apply is reverted in reverse order inside the
        caller's transaction. Each inverse runs only if its item is still at the
        version the Assistant left it in; within the group, an earlier action on the
        same item may build on the version this undo itself produced (they were one
        transaction, so nothing else can sit between them). Any newer change anywhere
        fails the whole undo instead of overwriting it.
        """
        latest = self.canonical.connection.execute(
            "SELECT apply_idempotency_key FROM assistant_action_history WHERE account_id=? AND principal_id=? "
            "AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (self.principal.account_id, self.principal.principal_id),
        ).fetchone()
        if latest is None:
            raise ValidationError("there is no reversible Assistant action to undo")
        if expected_apply is not None and latest["apply_idempotency_key"] != expected_apply:
            # A stale [Отменить] must not undo a newer Assistant change instead.
            raise VersionConflict("a newer Assistant change exists; undo that one first")
        rows = self.canonical.connection.execute(
            "SELECT * FROM assistant_action_history WHERE account_id=? AND principal_id=? "
            "AND apply_idempotency_key=? AND undone_at IS NULL ORDER BY sequence_index DESC, id DESC",
            (self.principal.account_id, self.principal.principal_id, latest["apply_idempotency_key"]),
        ).fetchall()
        from student_execution_os.sync.commands import APPLIED, NOOP, Commands
        commands = Commands(self.canonical, account_id=self.principal.account_id, actor=ActorCategory.USER_VIA_LLM,
                            now=self.canonical.clock.now())
        produced: dict[str, int] = {}
        undone: list[dict[str, Any]] = []
        for row in rows:
            inverse = json.loads(row["inverse_json"])
            operation = str(inverse.get("operation") or "")
            entity_id = str(row["entity_id"])
            expected = produced.get(entity_id, int(row["committed_version"]))
            if self._current_version(operation, entity_id) != expected:
                raise VersionConflict("Assistant undo conflicts with a newer entity version")
            outcome = commands.run(operation, entity_id, dict(inverse.get("payload") or {}))
            if outcome.status not in {APPLIED, NOOP}:
                raise ValidationError(outcome.message or outcome.code or "Assistant undo was not applied")
            self.canonical.connection.execute(
                "UPDATE assistant_action_history SET undone_at=? WHERE id=? AND undone_at IS NULL",
                (_iso(self.canonical.clock.now()), row["id"]),
            )
            after = self._current_version(operation, entity_id)
            if after is not None:
                produced[entity_id] = after
            step = {"entity_id": entity_id, "operation": operation, "outcome": outcome.status,
                    "entity": outcome.entity, "undid_action_id": row["action_id"]}
            if isinstance(outcome.entity, dict):
                step.update(version=outcome.entity.get("version"), status=outcome.entity.get("status"),
                            deleted=bool(outcome.entity.get("deleted")))
            undone.append(step)
        # The latest action first, as before; the full list for a multi-action apply.
        return {**undone[0], "undone": undone}
