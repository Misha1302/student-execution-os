"""Assistant actions for check-ins and recurring reminders (schema v31, ADR 0034).

The model (or the local parser) proposes; this module validates the typed payload
against canonical state and resolves *which occurrence* is meant — that choice is
the server's, never the model's: «я витамин уже принял» picks today's open dose
nearest to now, «вечерний приём пропущу» the open one in the evening. The model
may not supply an occurrence id the rule does not produce, and nothing here records
an outcome: apply goes through the same sync handlers as a button press.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.checkins import CheckInKind, SQLiteCheckInRepository, clean_template_fields
from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.recurrence import RecurrenceRule, resolve_local

from .disambiguation import same_word, title_score, tokens
from .model import AgentCommand

CREATE_CHECKIN_FIELDS = {"kind", "title", "dose_text", "instructions", "target_quantity", "unit", "unit_effort_seconds",
                         "dtstart_local", "recurrence_rule", "timezone_name", "remind", "delivery", "followup_minutes",
                         "window_minutes"}
CREATE_SERIES_FIELDS = {"title", "note", "dtstart_local", "recurrence_rule", "timezone_name", "delivery"}
OCCURRENCE_FIELDS = {"original_recurrence_id", "local_date", "day_part"}
DAY_PARTS = {"MORNING": (5, 12), "AFTERNOON": (12, 17), "EVENING": (17, 24), "NIGHT": (0, 5)}
TARGETED = {AgentCommand.CHECKIN_OUTCOME.value, AgentCommand.CHECKIN_PROGRESS.value,
            AgentCommand.MOVE_CHECKIN_OCCURRENCE.value}


def _local_civil(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"assistant {field} must be a local ISO datetime") from exc
    if parsed.tzinfo is not None:
        raise ValidationError(f"assistant {field} must be local civil time without an offset")
    return parsed.replace(second=0, microsecond=0)


def _zone(value: object, fallback: str) -> str:
    name = str(value or fallback)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("assistant timezone_name is not a known IANA zone") from exc
    return name


def validate_create(command: str, payload: dict[str, Any], *, timezone_name: str) -> None:
    """Shape and invariants of CREATE_CHECKIN / CREATE_REMINDER_SERIES (mutates defaults in)."""
    if "dtstart_local" in payload and payload["dtstart_local"] is not None:
        payload["dtstart_local"] = _local_civil(payload["dtstart_local"], "dtstart_local").strftime("%Y-%m-%dT%H:%M")
    if payload.get("recurrence_rule"):
        RecurrenceRule.parse(str(payload["recurrence_rule"]), allow_weekdays=True)
    payload["timezone_name"] = _zone(payload.get("timezone_name"), timezone_name)
    if command == AgentCommand.CREATE_CHECKIN.value:
        try:
            kind = CheckInKind(payload.get("kind") or CheckInKind.ROUTINE.value)
        except ValueError as exc:
            raise ValidationError("assistant check-in kind must be ROUTINE, MEDICATION or QUOTA") from exc
        payload["kind"] = kind.value
        fields = {key: payload[key] for key in payload if key not in {"kind", "dtstart_local", "recurrence_rule",
                                                                       "timezone_name"}}
        clean_template_fields(fields, kind=kind, creating=True)
    else:
        from student_execution_os.reminders.standalone import DELIVERIES
        if payload.get("delivery") is not None and payload["delivery"] not in DELIVERIES:
            raise ValidationError("assistant delivery must be PUSH, ALARM or PUSH_AND_ALARM")
        if payload.get("note") is not None and (not isinstance(payload["note"], str) or len(payload["note"]) > 2000):
            raise ValidationError("assistant note must be text up to 2000 characters")


def target_version(canonical: SQLiteCanonicalRepository, account_id: str, checkin_id: str) -> int | None:
    row = canonical.connection.execute(
        "SELECT version FROM checkin_templates WHERE account_id=? AND id=?", (account_id, checkin_id)).fetchone()
    return None if row is None else int(row["version"])


def resolve_occurrence(canonical: SQLiteCanonicalRepository, account_id: str, command: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
    """Pick the occurrence an action means; writes ``original_recurrence_id`` into the payload.

    Explicit identity must exist in the rule. Otherwise: the given (or implied) local
    day, an optional day part, then the open occurrence nearest to now — past ones
    first for «уже принял», since a dose is usually recorded after it was taken.
    """
    store = SQLiteCheckInRepository(canonical)
    template = store.get_template(account_id, str(payload["checkin_id"]))
    now = canonical.clock.now()
    zone = ZoneInfo(template.timezone_name)
    if payload.get("original_recurrence_id"):
        occurrence = store.require_occurrence(account_id, template.id, str(payload["original_recurrence_id"]))
    else:
        if payload.get("local_date"):
            try:
                day = date.fromisoformat(str(payload["local_date"]))
            except ValueError as exc:
                raise ValidationError("assistant local_date must be YYYY-MM-DD") from exc
        elif command == AgentCommand.MOVE_CHECKIN_OCCURRENCE.value and payload.get("when"):
            day = _instant(payload["when"]).astimezone(zone).date()
        else:
            day = now.astimezone(zone).date()
        start = datetime.combine(day, datetime.min.time(), zone)
        store.ensure_horizon(account_id, now)
        pairs = [(t, o) for t, o in store.occurrences_between(account_id, start - timedelta(hours=12),
                                                              start + timedelta(days=1, hours=12))
                 if t.id == template.id and datetime.fromisoformat(o.original_recurrence_id).date() == day]
        part = payload.get("day_part")
        if part is not None:
            if part not in DAY_PARTS:
                raise ValidationError("assistant day_part must be MORNING, AFTERNOON, EVENING or NIGHT")
            low, high = DAY_PARTS[part]
            pairs = [(t, o) for t, o in pairs if low <= o.scheduled_local.hour < high]
        wanted = {"PENDING", "MISSED"} if command != AgentCommand.MOVE_CHECKIN_OCCURRENCE.value else {"PENDING"}
        if command == AgentCommand.CHECKIN_PROGRESS.value:
            wanted = {"PENDING", "MISSED", "DONE"}
        open_pairs = [(t, o) for t, o in pairs if o.status.value in wanted]
        if not open_pairs:
            raise ValidationError("there is no open occurrence of this check-in that day")
        soon = now + timedelta(minutes=60)
        past = [(t, o) for t, o in open_pairs if store.scheduled_at(t, o) <= soon]
        pool = past or open_pairs
        occurrence = min(pool, key=lambda pair: abs((store.scheduled_at(*pair) - now).total_seconds()))[1]
    payload["original_recurrence_id"] = occurrence.original_recurrence_id
    payload.pop("local_date", None)
    payload.pop("day_part", None)
    scheduled = store.scheduled_at(template, occurrence)
    resolution = {"occurrence": occurrence.original_recurrence_id, "scheduled_at": scheduled.isoformat(),
                  "status": occurrence.status.value, "checkin_kind": template.kind.value, "title": template.title}
    if command == AgentCommand.MOVE_CHECKIN_OCCURRENCE.value:
        moved = _instant(payload["when"]).astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)
        resolve_local(moved, template.timezone_name)
        payload["target_local"] = moved.strftime("%Y-%m-%dT%H:%M")
        resolution["target_local"] = payload["target_local"]
    return resolution


def _instant(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("assistant time must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        raise ValidationError("assistant time must include an offset")
    return parsed


def validate_targeted(command: str, payload: dict[str, Any]) -> None:
    if command == AgentCommand.CHECKIN_OUTCOME.value:
        if payload.get("outcome") not in {"DONE", "SKIPPED"}:
            raise ValidationError("assistant check-in outcome must be DONE or SKIPPED")
        if payload.get("occurred_at") is not None:
            _instant(payload["occurred_at"])
    if command == AgentCommand.CHECKIN_PROGRESS.value:
        count = payload.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 100_000:
            raise ValidationError("assistant check-in count must be a positive whole number")
    if command == AgentCommand.MOVE_CHECKIN_OCCURRENCE.value:
        _instant(payload.get("when"))


def operation(command: str, data: dict[str, Any], new_id) -> tuple[str, str, dict[str, Any]]:
    """The sync operation an action stands for (same handlers as the buttons)."""
    if command == AgentCommand.CREATE_CHECKIN.value:
        return "checkin.create", new_id("checkin"), {k: v for k, v in data.items() if k in CREATE_CHECKIN_FIELDS}
    if command == AgentCommand.CREATE_REMINDER_SERIES.value:
        return "reminder_series.create", new_id("series"), {k: v for k, v in data.items() if k in CREATE_SERIES_FIELDS}
    identity = {"template_id": data["checkin_id"], "original_recurrence_id": data["original_recurrence_id"]}
    if command == AgentCommand.CHECKIN_OUTCOME.value:
        if data["outcome"] == "DONE":
            body = {**identity, **({"occurred_at": data["occurred_at"]} if data.get("occurred_at") else {})}
            return "checkin.occurrence.done", data["checkin_id"], body
        return "checkin.occurrence.skip", data["checkin_id"], {**identity, **({"note": data["note"]} if data.get("note") else {})}
    if command == AgentCommand.CHECKIN_PROGRESS.value:
        return "checkin.occurrence.progress", data["checkin_id"], {**identity, "count": data["count"]}
    return "checkin.occurrence.move", data["checkin_id"], {**identity, "target_local": data["target_local"]}


def context_items(canonical: SQLiteCanonicalRepository, account_id: str, now: datetime) -> list[dict[str, Any]]:
    """Check-ins the model may address: names and today's open days, no outcomes history."""
    store = SQLiteCheckInRepository(canonical)
    out = []
    for template in store.list_templates(account_id):
        if template.status != "ACTIVE":
            continue
        zone = ZoneInfo(template.timezone_name)
        start = datetime.combine(now.astimezone(zone).date(), datetime.min.time(), zone)
        today = [{"original_recurrence_id": o.original_recurrence_id, "scheduled_at": store.scheduled_at(t, o).isoformat(),
                  "status": o.status.value}
                 for t, o in store.occurrences_between(account_id, start, start + timedelta(days=1)) if t.id == template.id]
        out.append({"id": template.id, "kind": "CHECKIN", "title": template.title, "checkin_kind": template.kind.value,
                    "version": template.version, "today": today,
                    **({"starts_at": today[0]["scheduled_at"]} if today else {})})
    return out[:40]


# ---- local (no model) outcome commands ---------------------------------------------------

_DONE = re.compile(r"(?<![0-9a-zа-я])(?:уже\s+)?(?:принял[аи]?|выпил[аи]?|сделал[аи]?|took|done with|have taken)(?![0-9a-zа-я])")
_SKIP = re.compile(r"(?<![0-9a-zа-я])(?:пропущу|пропустил[аи]?|не\s+принял[аи]?|не\s+буду\s+принимать|не\s+выпил[аи]?"
                   r"|skip(?:ping)?|didn'?t\s+take|did\s+not\s+take)(?![0-9a-zа-я])")
_PARTS = [(re.compile(r"утренн|утром|morning"), "MORNING"), (re.compile(r"дневн|днем|днём|afternoon"), "AFTERNOON"),
          (re.compile(r"вечерн|вечером|evening|tonight"), "EVENING"), (re.compile(r"ночн|на ночь|night"), "NIGHT")]


def parse_outcome(text: str, checkins: list[dict[str, Any]]) -> dict[str, Any] | None:
    """«Я витамин уже принял», «сегодня вечерний приём пропущу» → CHECKIN_OUTCOME."""
    low = " ".join(str(text or "").lower().replace("ё", "е").split())
    skip = _SKIP.search(low)
    done = None if skip else _DONE.search(low)
    if not (skip or done) or not checkins:
        return None
    said = tokens(low)
    scored = sorted(((title_score(item["title"], said), item) for item in checkins), key=lambda pair: pair[0], reverse=True)
    best = scored[0][0] if scored else (0.0, 0)
    fitting = [item for score, item in scored if score == best and score[0] >= 0.5]
    generic = any(any(same_word(word, w) for w in ("прием", "лекарство", "таблетку", "таблетки", "dose", "pill"))
                  for word in said)
    if not fitting and generic:
        fitting = [item for item in checkins if item.get("checkin_kind") == "MEDICATION"]
    if not fitting:
        return None
    payload: dict[str, Any] = {"outcome": "SKIPPED" if skip else "DONE", "target_text": text.strip()[:300]}
    for pattern, part in _PARTS:
        if pattern.search(low):
            payload["day_part"] = part
            break
    unresolved: list[str] = []
    expected = None
    if len(fitting) == 1:
        payload["checkin_id"] = fitting[0]["id"]
        expected = int(fitting[0].get("version") or 1)
    else:
        unresolved = ["target", "expected_version"]
    return {"command": AgentCommand.CHECKIN_OUTCOME.value, "payload": payload, "confidence": 0.8,
            "unresolved_fields": unresolved, "expected_version": expected, "requires_confirmation": False}


def recurring_actions(parsed: dict[str, Any], timezone_name: str) -> list[dict[str, Any]]:
    """CREATE_CHECKIN / CREATE_REMINDER_SERIES actions for a local recurring reading.

    Two times («в 9 и в 21») are two series: each day has two separately recorded doses.
    """
    times = parsed.get("times") or [None]
    actions = []
    for index, clock in enumerate(times):
        start = parsed.get("dtstart_local")
        if clock and start and index > 0:
            start = f"{start[:10]}T{clock}"
        if parsed["kind"] == "CHECKIN":
            payload = {"kind": parsed["checkin_kind"], "title": parsed["title"], "recurrence_rule": parsed["recurrence_rule"],
                       "timezone_name": timezone_name}
            for key in ("dose_text", "target_quantity", "unit", "remind"):
                if parsed.get(key) is not None:
                    payload[key] = parsed[key]
            command = AgentCommand.CREATE_CHECKIN.value
        else:
            payload = {"title": parsed["title"], "recurrence_rule": parsed["recurrence_rule"], "timezone_name": timezone_name}
            command = AgentCommand.CREATE_REMINDER_SERIES.value
        if start:
            payload["dtstart_local"] = start
        actions.append({"command": command, "payload": payload, "confidence": 0.85,
                        "unresolved_fields": [] if start else ["dtstart_local"],
                        "expected_version": None, "requires_confirmation": False})
    return actions

