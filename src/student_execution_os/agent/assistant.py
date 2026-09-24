from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from typing import Any, Protocol
from uuid import uuid4

from student_execution_os.domain.errors import AuthorizationDenied, IdempotencyConflict, ValidationError, VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    HardCutoff,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    ObligationCategory,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .model import AgentCommand, AuthenticatedPrincipal
from .providers import ProviderUnavailable


COMMANDS = {command.value for command in AgentCommand}
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
        lowered = clean.lower()
        for prefix, command in (
            ("cancel ", AgentCommand.CANCEL_OBLIGATION), ("отмени ", AgentCommand.CANCEL_OBLIGATION),
            ("complete ", AgentCommand.COMPLETE_OBLIGATION), ("готово ", AgentCommand.COMPLETE_OBLIGATION),
        ):
            if lowered.startswith(prefix):
                target = clean[len(prefix):].strip()
                match = _resolve_target(target, context.get("obligations"))
                if match is None:
                    return [{"command": command.value, "payload": {"obligation_id": target}, "confidence": 0.5,
                             "unresolved_fields": ["obligation_id", "expected_version"],
                             "expected_version": None, "requires_confirmation": True}]
                return [{"command": command.value, "payload": {"obligation_id": match["id"]}, "confidence": 0.85,
                         "unresolved_fields": [], "expected_version": match["version"], "requires_confirmation": True}]
        raise ValidationError("input is not supported by the deterministic RU/EN parser")


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
_PAYLOAD_KEYS = {
    AgentCommand.CREATE_TASK.value: {"title", "description", "category", "importance", "estimated_total_effort_minutes",
                                     "remaining_effort_minutes", "actual_cutoff", "splittable"},
    AgentCommand.CREATE_EVENT.value: {"title", "description", "starts_at", "ends_at", "category", "importance",
                                      "attendance_policy", "location_effect", "arrival_requirement_minutes"},
    AgentCommand.REFINE_TASK.value: {"obligation_id", "estimated_total_effort_minutes", "activate"},
    AgentCommand.LOG_PROGRESS.value: {"obligation_id", "minutes"},
    AgentCommand.COMPLETE_OBLIGATION.value: {"obligation_id"},
    AgentCommand.CANCEL_OBLIGATION.value: {"obligation_id"},
}
_REQUIRED = {
    AgentCommand.CREATE_TASK.value: ("title",),
    AgentCommand.CREATE_EVENT.value: ("title", "starts_at", "ends_at"),
    AgentCommand.REFINE_TASK.value: ("obligation_id", "estimated_total_effort_minutes"),
    AgentCommand.LOG_PROGRESS.value: ("obligation_id", "minutes"),
    AgentCommand.COMPLETE_OBLIGATION.value: ("obligation_id",),
    AgentCommand.CANCEL_OBLIGATION.value: ("obligation_id",),
}


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
    for field in _REQUIRED[command]:
        if payload.get(field) in (None, "") and field not in unresolved:
            raise ValidationError(f"assistant {command} payload lacks {field}")
    if "title" in payload:
        title = payload["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValidationError("assistant proposal title must be 1-300 characters")
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
    expected = raw["expected_version"]
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise ValidationError("assistant expected_version must be an integer")
    target = payload.get("obligation_id")
    if target not in (None, "") and "obligation_id" not in unresolved:
        # Never let a model address something that does not exist in this account.
        row = canonical.connection.execute(
            "SELECT version FROM obligations WHERE account_id=? AND id=?", (account_id, str(target))
        ).fetchone()
        if row is None:
            raise ValidationError("assistant proposal references an unknown obligation")
        if expected is None and "expected_version" not in unresolved:
            raise ValidationError("assistant proposal on an existing obligation needs expected_version")
    return {"command": command, "payload": payload, "confidence": confidence,
            "unresolved_fields": list(unresolved), "expected_version": expected,
            "requires_confirmation": raw["requires_confirmation"]}


class SQLiteAssistantService:
    def __init__(self, canonical: SQLiteCanonicalRepository, principal: AuthenticatedPrincipal,
                 provider: AssistantProvider | None = None) -> None:
        self.canonical = canonical
        self.principal = principal
        self.provider = provider or DeterministicAssistantParser()

    def _context(self, client: dict[str, object]) -> dict[str, object]:
        """Server-owned context: the model only sees what the account already owns."""
        rows = self.canonical.connection.execute(
            "SELECT id,kind,title,version,lifecycle_status FROM obligations WHERE account_id=? "
            "AND lifecycle_status IN ('ACTIVE','DRAFT') ORDER BY updated_at DESC LIMIT 60",
            (self.principal.account_id,),
        ).fetchall()
        from student_execution_os.reminders import ReminderStore
        prefs = ReminderStore(self.canonical).prefs(self.principal.account_id)
        context: dict[str, object] = {
            "now": _iso(self.canonical.clock.now()), "timezone": prefs.timezone_name,
            "obligations": [{"id": row["id"], "kind": row["kind"], "title": row["title"], "version": int(row["version"]),
                             "status": row["lifecycle_status"]} for row in rows],
        }
        if isinstance(client.get("locale"), str):
            context["locale"] = client["locale"][:16]
        return context

    def interpret(self, text: str, context: dict[str, object] | None = None) -> dict[str, object]:
        if not str(text or "").strip():
            raise ValidationError("assistant input text is required")
        if len(str(text)) > 4000:
            raise ValidationError("assistant input is longer than 4000 characters")
        server_context = self._context(context if isinstance(context, dict) else {})
        provider_name = self.provider.name
        fallback = False
        try:
            interpretation = self.provider.interpret(text, server_context)
        except ProviderUnavailable:
            # Provider outage degrades to the local parser instead of failing the user.
            local = DeterministicAssistantParser()
            try:
                interpretation = local.interpret(text, server_context)
            except ValidationError:
                raise ValidationError("the language model is unavailable and the local parser did not understand the input") from None
            provider_name, fallback = local.name, True
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
        actions = []
        for raw in raw_actions:
            clean = validate_proposal(raw, self.canonical, self.principal.account_id)
            command = clean["command"]
            destructive = command in {
                AgentCommand.COMPLETE_OBLIGATION.value,
                AgentCommand.CANCEL_OBLIGATION.value,
            }
            actions.append({
                "id": str(uuid4()), **clean,
                "provenance": {"provider": provider_name, "input": "user-authored-text"},
                # Trust boundaries are server-owned. A provider cannot downgrade a
                # destructive command merely by emitting a false flag.
                "requires_confirmation": destructive or clean["requires_confirmation"],
            })
        now = self.canonical.clock.now()
        batch_id = str(uuid4())
        redacted = re.sub(r"\b[\w.+-]+@[\w.-]+\b", "[email]", text)[:2000]
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO assistant_batches(id,account_id,principal_id,input_hash,provider,redacted_input,actions_json,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, self.principal.account_id, self.principal.principal_id, digest, provider_name,
                 redacted, json.dumps(actions, sort_keys=True), _iso(now), _iso(now + timedelta(minutes=30))),
            )
        return {"batch_id": batch_id, "provider": provider_name, "fallback": fallback, "message": assistant_message, "actions": actions,
                "created_at": _iso(now), "expires_at": _iso(now + timedelta(minutes=30)), "mutated_canonical_state": False}

    def apply(self, payload: dict[str, object]) -> dict[str, object]:
        batch_id = str(payload.get("batch_id", ""))
        key = str(payload.get("idempotency_key", ""))
        selected = payload.get("action_ids")
        confirmed = set(payload.get("confirmed_action_ids") or [])
        if not batch_id or not key or not isinstance(selected, list) or not selected:
            raise ValidationError("batch_id, non-empty action_ids and idempotency_key are required")
        request_hash = hashlib.sha256(json.dumps({"batch_id": batch_id, "action_ids": selected, "confirmed": sorted(confirmed)}, sort_keys=True).encode()).hexdigest()
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
        actions = [by_id[action_id] for action_id in selected]
        if any(action["unresolved_fields"] for action in actions):
            raise ValidationError("unresolved proposal fields must be refined before apply")
        if any(action["requires_confirmation"] and action["id"] not in confirmed for action in actions):
            raise AuthorizationDenied("destructive or ambiguous action requires explicit confirmation")
        for action in actions:
            entity = action["payload"].get("obligation_id")
            if entity:
                current = self.canonical.get_obligation(self.principal.account_id, entity)
                if action["expected_version"] is None or current.version != int(action["expected_version"]):
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

    def _execute(self, action: dict[str, object]) -> dict[str, object]:
        command = AgentCommand(action["command"])
        data = action["payload"]
        common = {"account_id": self.principal.account_id, "actor": ActorCategory.USER_VIA_LLM}
        if command is AgentCommand.CREATE_TASK:
            effort = data.get("estimated_total_effort_minutes")
            task = self.canonical.create_task(
                **common, title=str(data["title"]), description=data.get("description"),
                category=ObligationCategory(data.get("category", "GENERAL")), importance=Importance(data.get("importance", "NORMAL")),
                estimated_total_effort_minutes=effort, remaining_effort_minutes=data.get("remaining_effort_minutes", effort),
                splittable=bool(data.get("splittable", False)), actual_cutoff=HardCutoff.unknown(),
            )
            from student_execution_os.reminders import ReminderStore
            ReminderStore(self.canonical).touch(self.principal.account_id, task.obligation.id, self.canonical.clock.now())
            return {"action_id": action["id"], "entity_id": task.obligation.id, "version": 1, "status": task.obligation.lifecycle_status.value}
        if command is AgentCommand.CREATE_EVENT:
            effect_data = data.get("location_effect") or {"kind": "NONE"}
            if not isinstance(effect_data, dict):
                raise ValidationError("event location_effect must be an object")
            event = self.canonical.create_event(
                **common,
                title=str(data["title"]),
                description=data.get("description"),
                starts_at=_dt(str(data["starts_at"])),
                ends_at=_dt(str(data["ends_at"])),
                time_semantics=EventTimeSemantics.FIXED_INTERVAL,
                category=ObligationCategory(data.get("category", "GENERAL")),
                importance=Importance(data.get("importance", "NORMAL")),
                attendance_policy=AttendancePolicy(data.get("attendance_policy", "REQUIRED")),
                location_effect=LocationEffect(
                    LocationEffectKind(effect_data.get("kind", "NONE")),
                    effect_data.get("origin_place_id"),
                    effect_data.get("destination_place_id"),
                ),
                arrival_requirement_minutes=int(data.get("arrival_requirement_minutes", 0)),
            )
            return {
                "action_id": action["id"], "entity_id": event.obligation.id,
                "version": event.obligation.version, "status": event.obligation.lifecycle_status.value,
            }
        entity = str(data["obligation_id"])
        expected = int(action["expected_version"])
        if command is AgentCommand.REFINE_TASK:
            task = self.canonical.update_task(**common, obligation_id=entity, expected_version=expected,
                estimated_total_effort_minutes=int(data["estimated_total_effort_minutes"]), activate=bool(data.get("activate", False)))
            return {"action_id": action["id"], "entity_id": entity, "version": task.obligation.version, "status": task.obligation.lifecycle_status.value}
        if command is AgentCommand.LOG_PROGRESS:
            task = self.canonical.get_task(self.principal.account_id, entity)
            minutes = int(data["minutes"])
            if task.remaining_effort_minutes is None:
                raise ValidationError("cannot log progress for unknown effort")
            updated = self.canonical.update_task(**common, obligation_id=entity, expected_version=expected,
                remaining_effort_minutes=max(0, task.remaining_effort_minutes - minutes),
                started_at=task.started_at or self.canonical.clock.now(), last_progress_at=self.canonical.clock.now())
            from student_execution_os.reminders import ReminderStore
            ReminderStore(self.canonical).touch(self.principal.account_id, entity, self.canonical.clock.now())
            return {"action_id": action["id"], "entity_id": entity, "version": updated.obligation.version, "status": updated.obligation.lifecycle_status.value}
        method = self.canonical.complete_obligation if command is AgentCommand.COMPLETE_OBLIGATION else self.canonical.cancel_obligation
        obligation = method(**common, obligation_id=entity, expected_version=expected)
        return {"action_id": action["id"], "entity_id": entity, "version": obligation.version, "status": obligation.lifecycle_status.value}
