"""One list of everything the user has committed to: tasks, events and reminders.

Task, Event and Reminder stay separate canonical entities with their own commands;
this is only a read projection ("Commitment") so the user sees them in one agenda
and one search. Each item keeps its canonical payload under ``entity``.

The device builds the same projection from its cached lists (web/static/js/
agenda.js), so the agenda and search also work offline; both are checked against
one fixture (tests/fixtures/commitments_cases.json).

    kind     TASK | EVENT | REMINDER | SHARED_EVENT | SHARED_OBLIGATION | ANNOUNCEMENT
    place    open | done | archive       — where the user finds it
    at       the item's own moment: a task's deadline (or target / start), an
             event's start, a reminder's moment; null when it has none
    at_kind  due | target | from | starts | remind | null

Group items (schema v18) come from the member's personal projection
(``/api/v1/me/shared``) and keep their own typed fields: ``attendance`` only for
events, ``criticality`` for events and deadlines, ``announcement_importance`` for
announcements (which have no time of their own: ``at`` stays null). A group
annotation of an event the member imported themselves is not a second row: it is
attached to that EVENT as ``annotation``. Items the member hid are not listed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

_TASK_PLACE = {"ACTIVE": "open", "DRAFT": "open", "COMPLETED": "done", "CANCELLED": "archive", "ARCHIVED": "archive"}
_REMINDER_PLACE = {"SCHEDULED": "open", "FIRED": "open", "DONE": "done", "CANCELLED": "archive"}


def _instant(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _annotation(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "title": item["title"], "event_kind": item["event_kind"], "group_name": item["group_name"],
            "criticality": item["criticality"]["effective"], "attendance": item["attendance"]["effective"],
            "status": item["status"]}


def shared_commitment(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    kind = item["kind"]
    extra: dict[str, Any] = {}
    at = at_kind = ends_at = None
    personal = item.get("personal") or {}
    if kind == "SHARED_EVENT":
        at, at_kind, ends_at = item["starts_at"], "starts", item["ends_at"]
        place = "archive" if item["status"] != "PUBLISHED" else "done" if _instant(ends_at) <= now else "open"
        extra = {"attendance": item["attendance"]["effective"], "criticality": item["criticality"]["effective"]}
        text = [item["title"], item.get("description"), item.get("location")]
    elif kind == "SHARED_OBLIGATION":
        at, at_kind = item["deadline"], "due"
        task = personal.get("personal_task") or {}
        place = "archive" if item["status"] != "PUBLISHED" or personal.get("acceptance_state") == "DECLINED" \
            else "done" if task.get("status") == "COMPLETED" else "open"
        extra = {"criticality": item["criticality"]["effective"]}
        text = [item["title"], item.get("description")]
    else:
        place = "archive" if item["status"] != "PUBLISHED" or personal.get("dismissed") else "open"
        extra = {"announcement_importance": item["importance"]}
        text = [item["title"], item.get("body")]
    return {
        "kind": kind, "id": item["id"], "title": item["title"], "status": item["status"], "place": place,
        "at": at, "at_kind": at_kind, "ends_at": ends_at, "importance": None, "version": item.get("version"),
        "source_label": item.get("group_name"), **extra,
        "search_text": " ".join(" ".join(filter(None, [*text, item.get("group_name")])).casefold().replace("ё", "е").split()),
        "entity": item,
    }


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
                now: datetime, place: str | None = None, query: str = "",
                shared: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    items = [commitment_of("TASK", task, now) for task in tasks]
    items += [commitment_of("EVENT", event, now) for event in events]
    items += [commitment_of("REMINDER", reminder, now) for reminder in reminders]
    local_events = {item["id"]: item for item in items if item["kind"] == "EVENT"}
    for entry in shared or []:
        if not entry.get("visible_in_agenda"):
            continue  # hidden by the member, a category they switched off, or an announcement not wanted here
        local = local_events.get(((entry.get("external") or {}).get("local_event_id")) or "")
        if entry["kind"] == "SHARED_EVENT" and local is not None:
            # One logical event: the member's own import, annotated by the group.
            local.setdefault("annotations", []).append(_annotation(entry))
            local["search_text"] = " ".join(filter(None, [local["search_text"], " ".join(
                f"{entry['title']} {entry['group_name']}".casefold().replace("ё", "е").split())]))
            continue
        items.append(shared_commitment(entry, now))
    words = " ".join(str(query or "").casefold().replace("ё", "е").split()).split()
    if words:
        items = [item for item in items if all(word in item["search_text"] for word in words)]
    if place:
        items = [item for item in items if item["place"] == place]
    return _order(items, place)
