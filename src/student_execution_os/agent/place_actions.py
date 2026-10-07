"""Assistant actions for places and location triggers (schema v32, ADR 0035).

Privacy boundary: the model sees places by name only (and the address only for a
place whose owner chose ASSISTANT_ADDRESS); it never sees or sends coordinates and
cannot reference a place outside the account. A trigger without a known place stays
unresolved for the user to pick — a place is never invented.
"""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.travel.repository import clean_place_fields
from student_execution_os.travel.triggers import clean_trigger_fields

from .model import AgentCommand

CREATE_PLACE_FIELDS = {"display_name", "alias", "address"}
CREATE_TRIGGER_FIELDS = {"place_id", "place_text", "transition", "title", "note"}


def context_places(canonical: SQLiteCanonicalRepository, account_id: str) -> list[dict[str, Any]]:
    rows = canonical.connection.execute(
        "SELECT id,alias,display_name,address,visibility_policy FROM places WHERE account_id=? "
        "ORDER BY coalesce(alias,display_name),id LIMIT 40", (account_id,)).fetchall()
    return [{"id": row["id"], "kind": "PLACE", "name": row["alias"] or row["display_name"],
             **({"address": row["address"]} if row["visibility_policy"] == "ASSISTANT_ADDRESS" and row["address"] else {})}
            for row in rows]


def _require_place(canonical: SQLiteCanonicalRepository, account_id: str, place_id: object) -> None:
    if not isinstance(place_id, str) or canonical.connection.execute(
            "SELECT 1 FROM places WHERE account_id=? AND id=?", (account_id, place_id)).fetchone() is None:
        raise ValidationError("assistant proposal references an unknown place")


def validate(command: str, payload: dict[str, Any], unresolved: list[str], canonical: SQLiteCanonicalRepository,
             account_id: str) -> None:
    if command == AgentCommand.CREATE_PLACE.value:
        clean_place_fields(payload, creating=True)
    elif command == AgentCommand.CREATE_LOCATION_TRIGGER.value:
        if payload.get("place_text") is not None and (not isinstance(payload["place_text"], str)
                                                      or len(payload["place_text"]) > 120):
            raise ValidationError("assistant place_text must be short text")
        fields = {k: v for k, v in payload.items() if k not in {"place_text", "place_id"}}
        clean_trigger_fields({**fields, "place_id": payload.get("place_id") or "pending"}, creating=True)
        if payload.get("place_id"):
            _require_place(canonical, account_id, payload["place_id"])
        elif "place_id" not in unresolved:
            raise ValidationError("assistant location trigger needs a place_id or an unresolved place")


def validate_event_location(payload: dict[str, Any], canonical: SQLiteCanonicalRepository, account_id: str) -> None:
    effect = payload.get("location_effect")
    if effect is None:
        return
    if not isinstance(effect, dict):
        raise ValidationError("assistant location_effect must be an object")
    for key in ("origin_place_id", "destination_place_id"):
        if effect.get(key) is not None:
            _require_place(canonical, account_id, effect[key])


def operation(command: str, data: dict[str, Any], new_id) -> tuple[str, str, dict[str, Any]]:
    if command == AgentCommand.CREATE_PLACE.value:
        return "place.create", new_id("place"), {k: v for k, v in data.items() if k in CREATE_PLACE_FIELDS}
    body = {k: v for k, v in data.items() if k in CREATE_TRIGGER_FIELDS - {"place_text"}}
    return "location_trigger.create", new_id("trigger"), body


def trigger_action(reading: dict[str, Any]) -> dict[str, Any]:
    payload = {"transition": reading["transition"], "title": reading["title"], "place_text": reading["place_text"]}
    if reading.get("place_id"):
        payload["place_id"] = reading["place_id"]
    return {"command": AgentCommand.CREATE_LOCATION_TRIGGER.value, "payload": payload, "confidence": 0.85,
            "unresolved_fields": list(reading.get("unresolved") or []), "expected_version": None,
            "requires_confirmation": False}


def place_action(reading: dict[str, Any]) -> dict[str, Any]:
    return {"command": AgentCommand.CREATE_PLACE.value, "payload": dict(reading), "confidence": 0.9,
            "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}
