"""One list of everything the user has committed to: tasks, events and reminders.

Task, Event and Reminder stay separate canonical entities with their own commands;
this is only a read projection ("Commitment") so the user sees them in one agenda
and one search. Each item keeps its canonical payload under ``entity``.

The device builds the same projection from its cached lists (web/static/js/
agenda.js), so the agenda and search also work offline; both are checked against
one fixture (tests/fixtures/commitments_cases.json).

    kind     TASK | EVENT | REMINDER
    place    open | done | archive       — where the user finds it
    at       the item's own moment: a task's deadline (or target / start), an
             event's start, a reminder's moment; null when it has none
    at_kind  due | target | from | starts | remind | null
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

_TASK_PLACE = {"ACTIVE": "open", "DRAFT": "open", "COMPLETED": "done", "CANCELLED": "archive", "ARCHIVED": "archive"}
_REMINDER_PLACE = {"SCHEDULED": "open", "FIRED": "open", "DONE": "done", "CANCELLED": "archive"}


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def commitment_of(kind: str, entity: dict[str, Any], now: datetime) -> dict[str, Any]:
    at: str | None = None
    at_kind: str | None = None
    ends_at: str | None = None
    if kind == "TASK":
        place = _TASK_PLACE.get(entity.get("status"), "open")
        cutoff = entity.get("actual_cutoff") or {}
        if cutoff.get("state") == "KNOWN" and cutoff.get("at"):
            at, at_kind = cutoff["at"], "due"
        elif entity.get("target_at"):
            at, at_kind = entity["target_at"], "target"
        elif entity.get("actionable_from"):
            at, at_kind = entity["actionable_from"], "from"
        text = " ".join(filter(None, [entity.get("title"), entity.get("description")]))
    elif kind == "EVENT":
        at, at_kind, ends_at = entity.get("starts_at"), "starts", entity.get("ends_at")
        ended = ends_at is not None and _instant(ends_at) <= now
        place = "archive" if entity.get("status") != "ACTIVE" else "done" if ended else "open"
        text = " ".join(filter(None, [entity.get("title"), entity.get("description")]))
    else:
        place = _REMINDER_PLACE.get(entity.get("status"), "open")
        at, at_kind = entity.get("remind_at"), "remind"
        text = " ".join(filter(None, [entity.get("title"), entity.get("note")]))
    return {
        "kind": kind, "id": entity["id"], "title": entity.get("title") or "", "status": entity.get("status"),
        "place": place, "at": at, "at_kind": at_kind, "ends_at": ends_at,
        "importance": entity.get("importance") or "NORMAL", "version": entity.get("version"),
        "search_text": " ".join(text.casefold().replace("ё", "е").split()),
        "entity": entity,
    }


def _order(items: list[dict[str, Any]], place: str | None) -> list[dict[str, Any]]:
    timed = [item for item in items if item["at"]]
    untimed = [item for item in items if not item["at"]]
    newest_first = place in ("done", "archive")
    timed.sort(key=lambda item: (_instant(item["at"]), item["kind"], item["id"]), reverse=newest_first)
    untimed.sort(key=lambda item: (item["title"].casefold(), item["id"]))
    return timed + untimed


def commitments(*, tasks: list[dict[str, Any]], events: list[dict[str, Any]], reminders: list[dict[str, Any]],
                now: datetime, place: str | None = None, query: str = "") -> list[dict[str, Any]]:
    items = [commitment_of("TASK", task, now) for task in tasks]
    items += [commitment_of("EVENT", event, now) for event in events]
    items += [commitment_of("REMINDER", reminder, now) for reminder in reminders]
    words = " ".join(str(query or "").casefold().replace("ё", "е").split()).split()
    if words:
        items = [item for item in items if all(word in item["search_text"] for word in words)]
    if place:
        items = [item for item in items if item["place"] == place]
    return _order(items, place)
