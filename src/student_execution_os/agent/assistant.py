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
                expected = context.get("expected_version")
                return [{"command": command.value, "payload": {"obligation_id": target}, "confidence": 0.75,
                         "unresolved_fields": ["expected_version"] if expected is None else [],
                         "expected_version": expected, "requires_confirmation": True}]
        raise ValidationError("input is not supported by the deterministic RU/EN parser")


class SQLiteAssistantService:
    def __init__(self, canonical: SQLiteCanonicalRepository, principal: AuthenticatedPrincipal,
                 provider: AssistantProvider | None = None) -> None:
        self.canonical = canonical
        self.principal = principal
        self.provider = provider or DeterministicAssistantParser()

    def interpret(self, text: str, context: dict[str, object] | None = None) -> dict[str, object]:
        context = context or {}
        allowed_context = {key: context[key] for key in ("expected_version", "locale") if key in context}
        interpretation = self.provider.interpret(text, allowed_context)
        if isinstance(interpretation, dict):
            raw_actions = interpretation.get("actions")
            assistant_message = str(interpretation.get("message") or "")[:2000]
        else:
            raw_actions = interpretation
            assistant_message = "I prepared a structured preview. Review it before applying."
        if not isinstance(raw_actions, list):
            raise ValidationError("assistant provider returned an invalid actions list")
        actions = []
        for raw in raw_actions:
            if set(raw) != {"command", "payload", "confidence", "unresolved_fields", "expected_version", "requires_confirmation"}:
                raise ValidationError("assistant provider returned an invalid typed action")
            command = str(raw["command"])
            if command not in COMMANDS or not isinstance(raw["payload"], dict):
                raise ValidationError("assistant provider returned an unknown action")
            confidence = float(raw["confidence"])
            if not 0 <= confidence <= 1:
                raise ValidationError("assistant confidence must be in [0,1]")
            destructive = command in {
                AgentCommand.COMPLETE_OBLIGATION.value,
                AgentCommand.CANCEL_OBLIGATION.value,
            }
            actions.append({
                "id": str(uuid4()), "command": command, "payload": raw["payload"],
                "confidence": confidence, "unresolved_fields": list(raw["unresolved_fields"]),
                "expected_version": raw["expected_version"],
                "provenance": {"provider": self.provider.name, "input": "user-authored-text"},
                # Trust boundaries are server-owned. A provider cannot downgrade a
                # destructive command merely by emitting a false flag.
                "requires_confirmation": destructive or bool(raw["requires_confirmation"]),
            })
        now = self.canonical.clock.now()
        batch_id = str(uuid4())
        redacted = re.sub(r"\b[\w.+-]+@[\w.-]+\b", "[email]", text)[:2000]
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO assistant_batches(id,account_id,principal_id,input_hash,provider,redacted_input,actions_json,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, self.principal.account_id, self.principal.principal_id, digest, self.provider.name,
                 redacted, json.dumps(actions, sort_keys=True), _iso(now), _iso(now + timedelta(minutes=30))),
            )
        return {"batch_id": batch_id, "provider": self.provider.name, "message": assistant_message, "actions": actions,
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
