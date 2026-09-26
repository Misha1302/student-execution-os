from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
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
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .model import AgentCommand, AuthenticatedPrincipal
from .commands import parse_command, reschedule_change
from .nlparse import parse_task
from .providers import ProviderUnavailable


COMMANDS = {command.value for command in AgentCommand}
_CREATES = {AgentCommand.CREATE_TASK.value, AgentCommand.CREATE_EVENT.value, AgentCommand.CREATE_REMINDER.value}
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
        # Everyday phrasing ("в пятницу к шести сдать лабу, часа два, важно").
        parsed = parse_task(str(text), now=now or datetime.now(timezone.utc), timezone_name=str(context.get("timezone") or "UTC"))
        if not parsed.get("title"):
            raise ValidationError("input is not supported by the deterministic RU/EN parser")
        unresolved = [field for field in parsed.pop("unresolved") if field != "title"]
        parsed.pop("cutoff_time_assumed", None)
        if parsed.get("kind") == "REMINDER":
            return [{"command": AgentCommand.CREATE_REMINDER.value, "payload": _reminder_payload(parsed), "confidence": 0.85,
                     "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}]
        if parsed.get("kind") == "EVENT":
            payload = {key: parsed[key] for key in ("title", "description", "starts_at", "ends_at", "category", "importance")
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


_ACTION_KEYS = {"command", "payload", "confidence", "unresolved_fields", "expected_version", "requires_confirmation"}
# Every field a CREATE_TASK proposal may carry maps onto the canonical task.create
# command (sync/commands.py); anything else is rejected by validate_proposal.
_CREATE_TASK_FIELDS = {
    "title", "description", "category", "importance", "estimated_total_effort_minutes", "remaining_effort_minutes",
    "actual_cutoff", "target_at", "actionable_from", "remind_at", "splittable", "min_chunk_minutes", "max_chunk_minutes",
}
_NULLABLE_CAPTURE = {"estimated_total_effort_minutes"}
_TARGET = {"obligation_id", "reminder_id", "target_text"}
_PAYLOAD_KEYS = {
    AgentCommand.CREATE_TASK.value: _CREATE_TASK_FIELDS,
    AgentCommand.CREATE_EVENT.value: {"title", "description", "starts_at", "ends_at", "category", "importance",
                                      "attendance_policy", "location_effect", "arrival_requirement_minutes",
                                      "remind_before_minutes"},
    AgentCommand.CREATE_REMINDER.value: {"title", "note", "remind_at", "delivery", "wake_check", "raise_volume", "obligation_id"},
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
    AgentCommand.RESCHEDULE.value: {"when", "keep_time"} | _TARGET,
    AgentCommand.SNOOZE.value: {"until"} | _TARGET,
}
_REQUIRED = {
    AgentCommand.CREATE_TASK.value: ("title",),
    AgentCommand.CREATE_EVENT.value: ("title", "starts_at", "ends_at"),
    AgentCommand.CREATE_REMINDER.value: ("title", "remind_at"),
    AgentCommand.REFINE_TASK.value: ("obligation_id", "estimated_total_effort_minutes"),
    AgentCommand.RESCHEDULE.value: ("when",),
    AgentCommand.SNOOZE.value: ("until",),
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


def validate_proposal(raw: object, canonical: SQLiteCanonicalRepository, account_id: str) -> dict[str, Any]:
    """Validate one provider action against the command schema and canonical state.

    Anything a model could get wrong — unknown commands or fields, wrong types,
    invented identifiers, bad dates — is rejected here, before the batch is stored,
    so it can neither be previewed nor applied. Fields the model honestly marks as
    unresolved are allowed to be missing; apply refuses them until refined.
    """
    if not isinstance(raw, dict) or set(raw) != _ACTION_KEYS:
        raise ValidationError("assistant provider returned an invalid typed action")
    command = raw["command"]
    payload = raw["payload"]
    if command not in COMMANDS or not isinstance(payload, dict):
        raise ValidationError("assistant provider returned an unknown action")
    from student_execution_os.groups import assistant as group_actions
    if command in group_actions.GROUP_COMMANDS:
        return _validate_group_proposal(raw, command, payload, canonical, account_id)
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
    for field in _REQUIRED.get(command, ()):
        if payload.get(field) in (None, "") and field not in unresolved:
            raise ValidationError(f"assistant {command} payload lacks {field}")
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValidationError("assistant proposal title must be 1-300 characters")
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
    if command == AgentCommand.RESCHEDULE.value:
        if payload.get("when") is not None:
            _instant(payload["when"], "when")
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
    if payload.get("target_text") is not None and (not isinstance(payload["target_text"], str) or len(payload["target_text"]) > 300):
        raise ValidationError("assistant target_text must be short text")
    expected = raw["expected_version"]
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValidationError("assistant expected_version must be an integer")
    if command in _TARGET_KINDS:
        _validate_target(command, payload, unresolved, expected, canonical, account_id)
    elif command == AgentCommand.CREATE_REMINDER.value and payload.get("obligation_id"):
        if canonical.connection.execute("SELECT 1 FROM obligations WHERE account_id=? AND id=?",
                                        (account_id, str(payload["obligation_id"]))).fetchone() is None:
            raise ValidationError("assistant proposal references an unknown obligation")
    return {"command": command, "payload": payload, "confidence": confidence,
            "unresolved_fields": list(unresolved), "expected_version": expected,
            "requires_confirmation": raw["requires_confirmation"]}


def _validate_group_proposal(raw: dict[str, Any], command: str, payload: dict[str, Any],
                             canonical: SQLiteCanonicalRepository, account_id: str) -> dict[str, Any]:
    """A group action: validated against the caller's memberships; group-wide ones always need confirmation."""
    from student_execution_os.groups import assistant as group_actions
    unresolved = raw["unresolved_fields"]
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        raise ValidationError("assistant unresolved_fields must be a list of field names")
    try:
        confidence = float(raw["confidence"])
    except (TypeError, ValueError) as exc:
        raise ValidationError("assistant confidence must be a number") from exc
    if not 0 <= confidence <= 1 or not isinstance(raw["requires_confirmation"], bool):
        raise ValidationError("assistant proposal has invalid confidence/confirmation")
    expected = raw["expected_version"]
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValidationError("assistant expected_version must be an integer")
    group_wide = group_actions.validate(command, payload, unresolved, expected, canonical, account_id)
    return {"command": command, "payload": payload, "confidence": confidence, "unresolved_fields": list(unresolved),
            "expected_version": expected, "requires_confirmation": group_wide or raw["requires_confirmation"]}


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
                 provider: AssistantProvider | None = None) -> None:
        self.canonical = canonical
        self.principal = principal
        self.provider = provider or DeterministicAssistantParser()

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
        from student_execution_os.groups import assistant as group_actions
        context.update(group_actions.context(self.canonical, self.principal.account_id, self.canonical.clock.now()))
        return context

    def interpret(self, text: str, context: dict[str, object] | None = None, *,
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
        self.provider_failure: ProviderUnavailable | None = None
        local = isinstance(self.provider, DeterministicAssistantParser)
        try:
            provider_name, message, actions = self._propose(self.provider, text, server_context)
        except ProviderUnavailable as exc:
            # Provider outage (or a rejected key) degrades to the local parser instead
            # of failing the user; the reason code tells the client why.
            self.provider_failure = exc
            provider_name, message, actions = self._local(text, server_context)
        except ValidationError as exc:
            if local or not degrade_invalid:
                raise
            self.provider_failure = ProviderUnavailable(str(exc), "INVALID_PROPOSAL")
            provider_name, message, actions = self._local(text, server_context)
        fallback = self.provider_failure is not None
        now = self.canonical.clock.now()
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
                "message": message, "actions": actions,
                "created_at": _iso(now), "expires_at": _iso(now + timedelta(minutes=30)), "mutated_canonical_state": False}

    def _local(self, text: str, context: dict[str, object]) -> tuple[str, str, list[dict[str, Any]]]:
        try:
            return self._propose(DeterministicAssistantParser(), text, context)
        except ValidationError:
            raise ValidationError("the language model is unavailable and the local parser did not understand the input") from None

    def _propose(self, provider: AssistantProvider, text: str,
                 context: dict[str, object]) -> tuple[str, str, list[dict[str, Any]]]:
        """Ask one provider and validate every action it proposes (nothing is stored)."""
        interpretation = provider.interpret(text, context)
        if isinstance(interpretation, dict):
            raw_actions = interpretation.get("actions")
            assistant_message = str(interpretation.get("message") or "")[:2000]
        else:
            raw_actions = interpretation
            assistant_message = "I prepared a structured preview. Review it before applying."
        if not isinstance(raw_actions, list):
            raise ValidationError("assistant provider returned an invalid actions list")
        if len(raw_actions) > 10:
            raise ValidationError("assistant proposed too many actions")
        raw_actions = self._reconcile_explicit_intent(text, context, raw_actions)
        actions = []
        for raw in raw_actions:
            clean = validate_proposal(raw, self.canonical, self.principal.account_id)
            actions.append({
                "id": str(uuid4()), **clean,
                "provenance": {"provider": provider.name, "input": "user-authored-text"},
                # Trust boundaries are server-owned. A provider cannot downgrade a
                # destructive command merely by emitting a false flag.
                "requires_confirmation": clean["command"] in DESTRUCTIVE or clean["requires_confirmation"],
            })
        return provider.name, assistant_message, actions

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
        actions = [self._edited(by_id[action_id], edits.get(action_id)) for action_id in selected]
        if any(action["unresolved_fields"] for action in actions):
            raise ValidationError("unresolved proposal fields must be refined before apply")
        if any(action["requires_confirmation"] and action["id"] not in confirmed for action in actions):
            raise AuthorizationDenied("destructive or ambiguous action requires explicit confirmation")
        for action in actions:
            if action["command"] not in _TARGET_KINDS:
                continue
            target = target_of(self.canonical, self.principal.account_id, action["payload"])
            if target is None:
                raise ValidationError("the item this action is about no longer exists")
            if action["expected_version"] is None or target[2] != int(action["expected_version"]):
                raise VersionConflict("assistant proposal expected version is stale or missing")
        with self.canonical._tx() as conn:
            results = [self._execute(action) for action in actions]
            result = {"batch_id": batch_id, "results": results, "replayed": False}
            conn.execute(
                "INSERT INTO assistant_apply_records(account_id,principal_id,idempotency_key,request_hash,result_json,created_at) VALUES (?,?,?,?,?,?)",
                (self.principal.account_id, self.principal.principal_id, key, request_hash,
                 json.dumps(result, sort_keys=True), _iso(self.canonical.clock.now())),
            )
        return result

    def _edited(self, action: dict[str, Any], edit: dict[str, Any] | None) -> dict[str, Any]:
        """Apply the user's answers/corrections from the preview card, then re-validate.

        Setting a field in ``edits`` resolves it, including an explicit "don't know"
        (``null`` effort → a draft; ``{"state": "UNKNOWN"}`` deadline).
        """
        if not edit:
            return action
        edit = dict(edit)
        raw = {key: action[key] for key in _ACTION_KEYS}
        if "expected_version" in edit:
            raw["expected_version"] = edit.pop("expected_version")
        payload = {**action["payload"], **edit}
        picked = "obligation_id" in edit or "reminder_id" in edit
        if picked:
            # The user chose which item the command is about.
            payload.pop("target_text", None)
            if "reminder_id" in edit:
                payload.pop("obligation_id", None)
        raw["payload"] = payload
        raw["unresolved_fields"] = [field for field in action["unresolved_fields"] if field not in edit
                                    and not (picked and field in {"target", "obligation_id", "reminder_id", "expected_version"})]
        clean = validate_proposal(raw, self.canonical, self.principal.account_id)
        return {**action, **clean, "requires_confirmation": action["requires_confirmation"]}

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
                               now=self.canonical.clock.now()).task_create(task_id, dict(data))
            return {"action_id": action["id"], "entity_id": task_id, "version": outcome.entity["version"],
                    "status": outcome.entity["status"], "entity": outcome.entity}
        if command is AgentCommand.REFINE_TASK:
            entity = str(data["obligation_id"])
            task = self.canonical.update_task(**common, obligation_id=entity, expected_version=int(action["expected_version"]),
                estimated_total_effort_minutes=int(data["estimated_total_effort_minutes"]), activate=bool(data.get("activate", False)))
            return {"action_id": action["id"], "entity_id": entity, "version": task.obligation.version, "status": task.obligation.lifecycle_status.value}
        # Everything else runs through the same command handlers as the offline sync
        # queue, so an Assistant action and a button press mean exactly the same thing.
        from student_execution_os.sync.commands import APPLIED, NOOP, Commands
        from student_execution_os.groups import assistant as group_actions
        commands = Commands(self.canonical, account_id=self.principal.account_id, actor=ActorCategory.USER_VIA_LLM,
                            now=self.canonical.clock.now())
        if command.value in group_actions.GROUP_COMMANDS:
            op_type, entity, body = group_actions.operation(command.value, dict(data), action.get("expected_version"),
                                                            str(action["id"]), self.canonical)
        else:
            op_type, entity, body = self._operation(command, data, commands)
        outcome = commands.run(op_type, entity, body, mutation_id=f"assistant:{action['id']}")
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
        fields = {key: value for key, value in data.items() if key not in _TARGET}
        if command is AgentCommand.CREATE_EVENT:
            # One event.create owner for manual, offline and Assistant creation, so a
            # requested reminder lead (remind_before_minutes) is stored the same way.
            return "event.create", f"event-{uuid4()}", fields
        if command is AgentCommand.CREATE_REMINDER:
            return "reminder.create", f"reminder-{uuid4()}", fields | ({"obligation_id": data["obligation_id"]} if data.get("obligation_id") else {})
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
            if kind == "REMINDER":
                current = commands._reminders().get(self.principal.account_id, entity)
            elif kind == "EVENT":
                current = commands._event_out(entity).entity
            else:
                current = commands._task_out(entity).entity
            op_type, body = reschedule_change(kind, current, _dt(str(fields["when"])), bool(fields.get("keep_time")), zone)
            return op_type, entity, body
        raise ValidationError(f"unsupported assistant command {command.value}")
