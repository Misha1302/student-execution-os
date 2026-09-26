"""Typed group actions for the existing text/voice Assistant boundary (no separate pipeline).

A model (or any interpreter) only *proposes* these; ``agent/assistant.py`` validates
them here against the caller's own memberships and capabilities, stores the preview,
and executes an action only after the user confirmed it, through the same group
operations as the buttons. Every group-wide action is forced to require explicit
confirmation whatever the model said: the Assistant never publishes to a group on
its own.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from .commands import GroupCommands
from .model import (
    Capability,
    SharedKind,
    UserSharedEventState,
    UserSharedObligationState,
    parse_event_payload,
    parse_obligation_payload,
    parse_payload,
    patch_event_state,
    patch_obligation_state,
)

_EVENT_FIELDS = {"title", "event_kind", "starts_at", "ends_at", "location", "attendance_default", "group_criticality",
                 "description"}
PAYLOAD_KEYS: dict[str, set[str]] = {
    "CREATE_SHARED_EVENT": {"group_id"} | _EVENT_FIELDS,
    "UPDATE_SHARED_EVENT": {"shared_event_id"} | _EVENT_FIELDS,
    "CANCEL_SHARED_EVENT": {"shared_event_id", "reason"},
    "CREATE_SHARED_OBLIGATION": {"group_id", "title", "obligation_kind", "deadline", "group_criticality", "description",
                                 "estimated_effort_hint_minutes"},
    "CREATE_GROUP_PROPOSAL": {"group_id", "proposal"},
    "APPROVE_GROUP_PROPOSAL": {"proposal_id", "comment"},
    "REJECT_GROUP_PROPOSAL": {"proposal_id", "comment"},
    "SET_PERSONAL_EVENT_PREFERENCES": {"shared_event_id", "attendance_override", "criticality_override",
                                       "remind_before_minutes", "alarm_before_minutes", "muted"},
    "SET_PERSONAL_OBLIGATION_PREFERENCES": {"shared_obligation_id", "acceptance_state", "criticality_override",
                                            "remind_before_minutes", "muted"},
    "CREATE_PREPARATION_TASK": {"shared_event_id", "shared_obligation_id", "title", "estimated_total_effort_minutes"},
}
GROUP_COMMANDS = frozenset(PAYLOAD_KEYS)
# Changes everyone in a group sees: always confirmed by the user in the preview.
GROUP_WIDE_COMMANDS = frozenset({"CREATE_SHARED_EVENT", "UPDATE_SHARED_EVENT", "CANCEL_SHARED_EVENT",
                                 "CREATE_SHARED_OBLIGATION", "CREATE_GROUP_PROPOSAL", "APPROVE_GROUP_PROPOSAL",
                                 "REJECT_GROUP_PROPOSAL"})
# Commands on an existing versioned group entity.
_VERSIONED = frozenset({"UPDATE_SHARED_EVENT", "CANCEL_SHARED_EVENT", "APPROVE_GROUP_PROPOSAL", "REJECT_GROUP_PROPOSAL"})

PROMPT = """Group commands (context.groups lists the user's groups with their capabilities; context.shared_items
the group events/deadlines they see, with "version"). Never invent ids. Every group-wide command sets
requires_confirmation=true:
  CREATE_SHARED_EVENT {group_id, title, event_kind (LECTURE|SEMINAR|PRACTICE|LAB|CLASS|CONSULTATION|QUIZ|TEST|
    CONTROL_WORK|COLLOQUIUM|EXAM|GROUP_MEETING|OTHER), starts_at, ends_at, location?, group_criticality?
    NORMAL|IMPORTANT|CRITICAL, attendance_default? REQUIRED|PREFERRED|OPTIONAL|SKIP} — only when the group lists
    PUBLISH_SHARED; otherwise CREATE_GROUP_PROPOSAL {group_id, proposal: {kind:"SHARED_EVENT", …same fields}}
    ("предложи группе квиз по АиСД в понедельник")
  UPDATE_SHARED_EVENT {shared_event_id, starts_at?, ends_at?, title?, …} + expected_version
    ("перенеси контрольную группы на 14:00")
  CANCEL_SHARED_EVENT {shared_event_id} + expected_version
  CREATE_SHARED_OBLIGATION {group_id, title, deadline, obligation_kind?, group_criticality?}
  APPROVE_GROUP_PROPOSAL / REJECT_GROUP_PROPOSAL {proposal_id} + expected_version
Personal (only for the user, the group never sees it):
  SET_PERSONAL_EVENT_PREFERENCES {shared_event_id, attendance_override?, criticality_override?,
    remind_before_minutes?, alarm_before_minutes?, muted?} ("для меня эта лекция необязательная" =
    attendance_override OPTIONAL)
  SET_PERSONAL_OBLIGATION_PREFERENCES {shared_obligation_id, acceptance_state? ACCEPTED|DECLINED, …}
  CREATE_PREPARATION_TASK {shared_event_id|shared_obligation_id, estimated_total_effort_minutes, title?}"""


def context(repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> dict[str, Any]:
    """What the Assistant may address: the caller's groups and the group items they see."""
    from .projection import PersonalProjection
    projection = PersonalProjection(repo, account_id, now)
    groups = [{"id": g["id"], "name": g["name"], "capabilities": g["capabilities"]}
              for g in projection.memberships() if g["my_membership"]["status"] == "ACTIVE"]
    items = [{"id": i["id"], "kind": i["kind"], "group_id": i["group_id"], "title": i["title"], "version": i["version"],
              **({"event_kind": i["event_kind"], "starts_at": i["starts_at"]} if i["kind"] == "SHARED_EVENT" else {}),
              **({"deadline": i["deadline"]} if i["kind"] == "SHARED_OBLIGATION" else {})}
             for i in projection.items() if i["kind"] != "ANNOUNCEMENT" and i["status"] == "PUBLISHED"][:60]
    return {"groups": groups, "shared_items": items}


def validate(command: str, payload: dict[str, Any], unresolved: list[str], expected: Any,
             repo: SQLiteCanonicalRepository, account_id: str) -> bool:
    """Check a proposed group action; returns whether it must be confirmed (always for group-wide ones)."""
    unknown = set(payload) - PAYLOAD_KEYS[command]
    if unknown:
        raise ValidationError(f"assistant {command} payload has unsupported fields: {', '.join(sorted(unknown))}")
    commands = GroupCommands(repo, account_id=account_id, now=repo.clock.now())
    try:
        _validate(command, payload, commands)
    except EntityNotFound as exc:
        raise ValidationError("assistant proposal references an unknown group item") from exc
    except DomainError as exc:
        raise ValidationError(f"assistant {command}: {exc}") from exc
    if command in _VERSIONED and expected is None and "expected_version" not in unresolved:
        raise ValidationError("assistant proposal on an existing group item needs expected_version")
    return command in GROUP_WIDE_COMMANDS


def _validate(command: str, payload: dict[str, Any], commands: GroupCommands) -> None:
    if command in ("CREATE_SHARED_EVENT", "CREATE_SHARED_OBLIGATION"):
        group, _, _ = commands.access(str(payload.get("group_id") or ""), Capability.PUBLISH_SHARED)
        fields = {k: v for k, v in payload.items() if k != "group_id"}
        parser = parse_event_payload if command == "CREATE_SHARED_EVENT" else parse_obligation_payload
        parser(fields, default_timezone=group.settings.default_timezone)
    elif command == "CREATE_GROUP_PROPOSAL":
        group, _, _ = commands.access(str(payload.get("group_id") or ""), Capability.CREATE_PROPOSAL)
        parse_payload(payload.get("proposal"), default_timezone=group.settings.default_timezone)
    elif command in ("UPDATE_SHARED_EVENT", "CANCEL_SHARED_EVENT"):
        commands._entity(SharedKind.SHARED_EVENT, str(payload.get("shared_event_id") or ""), Capability.PUBLISH_SHARED)
    elif command in ("APPROVE_GROUP_PROPOSAL", "REJECT_GROUP_PROPOSAL"):
        commands._proposal(str(payload.get("proposal_id") or ""), Capability.MODERATE_PROPOSALS)
    elif command == "SET_PERSONAL_EVENT_PREFERENCES":
        entity = str(payload.get("shared_event_id") or "")
        commands._entity(SharedKind.SHARED_EVENT, entity, Capability.VIEW_SHARED, writable=False)
        patch_event_state(UserSharedEventState("-", entity), {k: v for k, v in payload.items() if k != "shared_event_id"})
    elif command == "SET_PERSONAL_OBLIGATION_PREFERENCES":
        entity = str(payload.get("shared_obligation_id") or "")
        commands._entity(SharedKind.SHARED_OBLIGATION, entity, Capability.VIEW_SHARED, writable=False)
        patch_obligation_state(UserSharedObligationState("-", entity),
                               {k: v for k, v in payload.items() if k != "shared_obligation_id"})
    elif command == "CREATE_PREPARATION_TASK":
        kind, entity = _prepared(payload)
        commands._entity(kind, entity, Capability.VIEW_SHARED, writable=False)
        minutes = payload.get("estimated_total_effort_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= 100_000:
            raise ValidationError("estimated_total_effort_minutes must be positive whole minutes")


def _prepared(payload: dict[str, Any]) -> tuple[SharedKind, str]:
    if bool(payload.get("shared_event_id")) == bool(payload.get("shared_obligation_id")):
        raise ValidationError("name exactly one shared event or shared deadline")
    if payload.get("shared_event_id"):
        return SharedKind.SHARED_EVENT, str(payload["shared_event_id"])
    return SharedKind.SHARED_OBLIGATION, str(payload["shared_obligation_id"])


def operation(command: str, payload: dict[str, Any], expected: Any, action_id: str,
              repo: SQLiteCanonicalRepository) -> tuple[str, str, dict[str, Any]]:
    """The group/personal operation (type, entity id, payload) a confirmed action stands for."""
    version = {} if expected is None else {"expected_version": int(expected)}
    if command == "CREATE_SHARED_EVENT":
        return "shared_event.create", f"sev-ai-{action_id}"[:120], dict(payload)
    if command == "CREATE_SHARED_OBLIGATION":
        return "shared_obligation.create", f"sob-ai-{action_id}"[:120], dict(payload)
    if command == "CREATE_GROUP_PROPOSAL":
        return "proposal.create", f"prp-ai-{action_id}"[:120], {"group_id": payload["group_id"], "payload": payload["proposal"]}
    if command == "UPDATE_SHARED_EVENT":
        fields = {k: v for k, v in payload.items() if k != "shared_event_id"}
        return "shared_event.update", str(payload["shared_event_id"]), {**fields, **version}
    if command == "CANCEL_SHARED_EVENT":
        return "shared_event.cancel", str(payload["shared_event_id"]), {**version, **({"reason": payload["reason"]}
                                                                                      if payload.get("reason") else {})}
    if command in ("APPROVE_GROUP_PROPOSAL", "REJECT_GROUP_PROPOSAL"):
        op = "proposal.approve" if command == "APPROVE_GROUP_PROPOSAL" else "proposal.reject"
        return op, str(payload["proposal_id"]), {**version, **({"comment": payload["comment"]} if payload.get("comment") else {})}
    if command == "SET_PERSONAL_EVENT_PREFERENCES":
        return "shared_event_state.update", str(payload["shared_event_id"]), \
            {k: v for k, v in payload.items() if k != "shared_event_id"}
    if command == "SET_PERSONAL_OBLIGATION_PREFERENCES":
        return "shared_obligation_state.update", str(payload["shared_obligation_id"]), \
            {k: v for k, v in payload.items() if k != "shared_obligation_id"}
    if command == "CREATE_PREPARATION_TASK":
        kind, entity = _prepared(payload)
        from .repository import SQLiteGroupRepository
        row = SQLiteGroupRepository(repo).entity(kind, entity)
        if row is None:
            raise EntityNotFound("shared item not found")
        body: dict[str, Any] = {"title": payload.get("title") or row["title"],
                                "estimated_total_effort_minutes": payload["estimated_total_effort_minutes"],
                                "splittable": payload["estimated_total_effort_minutes"] > 60,
                                "prepares": {"kind": kind.value, "id": entity}}
        return "task.create", f"task-ai-{action_id}"[:120], body
    raise ValidationError(f"unsupported group command {command}")
