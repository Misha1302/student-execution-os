"""Commands about things the user already has, in everyday RU/EN phrasing.

    "готово эссе", "эссе в архив", "отмени созвон", "перенеси эссе на завтра в 18",
    "напомни про эссе через час", "поработал над эссе 30 минут", "сделал 3 задачи по матану"

``parse_command`` turns such a phrase into one typed Assistant action (the same
schema a language model must produce) or returns ``None`` so the text is read as
something new to create. It never executes anything: every action is previewed and,
for destructive ones, confirmed by the user.

The device runs an identical grammar (web/static/js/commands.js) against the same
fixture (tests/fixtures/nl_command_cases.json), so commands also work offline.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .nlparse import parse_task

_PUNCT = re.compile(r"[\"'«»“”„()!?.,;:]+")
_LEAD_PREPS = re.compile(r"^(?:над|по|на|про|о|об|с|со|задачу|задача|событие|напоминание|on|for|about|of|at|the|task|event|reminder)\s+", re.I)
_CLOCK_WORDS = re.compile(r"\d{1,2}[:.]\d{2}|(?<!\w)(?:в|во|к|at|by)\s+\d{1,2}(?!\d)|\d\s*(?:am|pm)|утр|вечер|дн[её]м|ноч|полдень|полночь"
                          r"|morning|evening|afternoon|night|noon|midnight|через|in\s+\d", re.I)

COMPLETE = re.compile(r"^(?:готово|сделано|сделал[аи]?|выполнил[аи]?|выполнено|закончил[аи]?|отметь(?:\s+как)?\s+выполненн\w*"
                      r"|done|complete|completed|finished|mark\s+done)\s*[-—:]?\s+(?P<target>.+)$", re.I)
COMPLETE_SUFFIX = re.compile(r"^(?P<target>.+?)\s*[-—:]?\s+(?:готово|сделано|выполнено|done)$", re.I)
ARCHIVE = re.compile(r"^(?:в\s+архив|архивируй|заархивируй|убери\s+в\s+архив|archive)\s*[-—:]?\s+(?P<target>.+)$", re.I)
ARCHIVE_SUFFIX = re.compile(r"^(?P<target>.+?)\s+в\s+архив$", re.I)
CANCEL = re.compile(r"^(?:отмени(?:ть)?|не\s+буду\s+делать|cancel|drop)\s*[-—:]?\s+(?P<target>.+)$", re.I)
MOVE = re.compile(r"^(?:перенеси(?:те)?|перенести|сдвинь|передвинь|move|reschedule|postpone)\s+(?P<rest>.+)$", re.I)
SNOOZE = re.compile(r"^(?:напомни(?:те)?(?:\s+мне)?\s+(?:про|о|об)|remind\s+me\s+(?:about|of)|отложи|snooze)\s+(?P<rest>.+)$", re.I)
PROGRESS = re.compile(r"^(?:поработал[аи]?|позанимал(?:ся|ась|ись)|занимал(?:ся|ась|ись)|потратил[аи]?|worked|spent|logged)\s+(?P<rest>.+)$", re.I)
COUNT = re.compile(r"^(?:сделал[аи]?|решил[аи]?|прочитал[аи]?|написал[аи]?|did|solved|read|finished)\s+(?P<n>\d{1,5})\s+(?P<unit>\S+)"
                   r"\s+(?:по|из|в|для|for|of|in|from)\s+(?P<target>.+)$", re.I)


def normalize(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", str(text or "").casefold().replace("ё", "е")).split())


def _stem(word: str) -> str:
    """A crude RU/EN stem: endings are vowels ("лабу"/"лаба", "матану"/"матан")."""
    return (word.rstrip("аеиоуыэюяйьeiouy") or word)[:5]


def _stems(text: str) -> set[str]:
    return {_stem(word) for word in normalize(text).split() if len(word) > 2 or word.isdigit()}


def score(fragment: str, title: str) -> float:
    """How well a spoken fragment names an item (1.0 exact … 0 unrelated)."""
    a, b = normalize(fragment), normalize(title)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 3 and (a in b or b in a):
        return 0.9
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    if sa <= sb:
        return 0.85
    return len(sa & sb) / len(sa | sb)


def match_target(fragment: str, items: list[dict[str, Any]], *, kinds: tuple[str, ...] = ("TASK", "EVENT", "REMINDER"),
                 statuses: tuple[str, ...] | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The single item the fragment names, or None plus the closest candidates."""
    pool = [item for item in items if item.get("kind") in kinds and (statuses is None or item.get("status") in statuses)]
    scored = sorted(((score(fragment, str(item.get("title", ""))), index, item) for index, item in enumerate(pool)),
                    key=lambda row: (-row[0], row[1]))
    candidates = [item for value, _, item in scored if value >= 0.34][:5]
    if not scored or scored[0][0] < 0.5:
        return None, candidates
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
        return None, candidates  # two items fit equally well: the user chooses
    return scored[0][2], candidates


def _clean_target(text: str) -> str:
    value = " ".join(str(text).split()).strip(" -—:,.")
    while True:
        stripped = _LEAD_PREPS.sub("", value)
        if stripped == value:
            return value
        value = stripped


def _moment(parsed: dict[str, Any]) -> datetime | None:
    for key in ("starts_at", "remind_at", "actionable_from", "target_at"):
        if parsed.get(key):
            return datetime.fromisoformat(parsed[key])
    cutoff = parsed.get("actual_cutoff") or {}
    return datetime.fromisoformat(cutoff["at"]) if cutoff.get("at") else None


def _action(command: str, payload: dict[str, Any], *, item: dict[str, Any] | None, target: str,
            confirm: bool = False, unresolved: list[str] | None = None) -> dict[str, Any]:
    missing = list(unresolved or [])
    if item is None:
        payload = {**payload, "target_text": target}
        missing = ["target", *missing]
    else:
        payload = {"reminder_id" if item["kind"] == "REMINDER" else "obligation_id": item["id"], **payload}
    return {"command": command, "payload": payload, "confidence": 0.9 if item else 0.5,
            "unresolved_fields": missing, "expected_version": None if item is None else item.get("version"),
            "requires_confirmation": confirm}


def reschedule_change(kind: str, entity: dict[str, Any], when: datetime, keep_time: bool,
                      zone: ZoneInfo) -> tuple[str, dict[str, Any]]:
    """The sync operation that moves a task, event or reminder to ``when``.

    ``keep_time``: only a day was said ("перенеси на завтра"), so the item keeps its
    own time of day. A task with a deadline moves its deadline; one without is put
    off until then; an event moves its start (its length is kept); a reminder moves
    its moment.
    """
    if kind == "EVENT":
        current = entity.get("starts_at")
    elif kind == "REMINDER":
        current = entity.get("remind_at")
    else:
        cutoff = entity.get("actual_cutoff") or {}
        current = cutoff.get("at") if cutoff.get("state") == "KNOWN" else entity.get("actionable_from")
    if keep_time and current:
        old = datetime.fromisoformat(current).astimezone(zone)
        when = datetime.combine(when.astimezone(zone).date(), old.time(), zone)
    value = when.astimezone(ZoneInfo("UTC")).isoformat()
    if kind == "EVENT":
        return "event.update", {"starts_at": value}
    if kind == "REMINDER":
        return "reminder.update", {"remind_at": value}
    cutoff = entity.get("actual_cutoff") or {}
    if cutoff.get("state") == "KNOWN":
        return "task.update", {"actual_cutoff": {"state": "KNOWN", "at": value, "boundary": cutoff.get("boundary") or "INCLUSIVE"}}
    return "task.defer", {"until": value}


def parse_command(text: str, *, now: datetime, timezone_name: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    raw = " ".join(str(text or "").split()).strip(" .!")
    if not raw:
        return None
    try:
        zone = ZoneInfo(timezone_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    open_ = ("ACTIVE", "DRAFT", "SCHEDULED", "FIRED")

    counted = COUNT.match(raw)
    if counted:
        target = _clean_target(counted.group("target"))
        item, _ = match_target(target, items, kinds=("TASK",), statuses=("ACTIVE", "DRAFT"))
        if item is not None or not items:
            return _action("LOG_PROGRESS", {"count": int(counted.group("n"))}, item=item, target=target)
    worked = PROGRESS.match(raw)
    if worked:
        parsed = parse_task(worked.group("rest"), now=now, timezone_name=zone.key)
        minutes = parsed.get("estimated_total_effort_minutes")
        target = _clean_target(parsed.get("title") or "")
        if minutes and target:
            item, _ = match_target(target, items, kinds=("TASK",), statuses=("ACTIVE", "DRAFT"))
            return _action("LOG_PROGRESS", {"minutes": int(minutes)}, item=item, target=target)
    for pattern, command, kinds, statuses in (
        (COMPLETE, "COMPLETE_OBLIGATION", ("TASK", "REMINDER"), open_),
        (ARCHIVE, "ARCHIVE_OBLIGATION", ("TASK",), None),
        (CANCEL, "CANCEL_OBLIGATION", ("TASK", "EVENT", "REMINDER"), open_),
        (COMPLETE_SUFFIX, "COMPLETE_OBLIGATION", ("TASK", "REMINDER"), open_),
        (ARCHIVE_SUFFIX, "ARCHIVE_OBLIGATION", ("TASK",), None),
    ):
        hit = pattern.match(raw)
        if hit:
            target = _clean_target(hit.group("target"))
            item, _ = match_target(target, items, kinds=kinds, statuses=statuses)
            if item is None and pattern in (COMPLETE_SUFFIX, ARCHIVE_SUFFIX):
                continue  # "купить хлеб готово"? only a known item makes a suffix a command
            return _action(command, {}, item=item, target=target, confirm=True)
    moved = MOVE.match(raw)
    if moved:
        parsed = parse_task(moved.group("rest"), now=now, timezone_name=zone.key)
        when = _moment(parsed)
        target = _clean_target(parsed.get("title") or moved.group("rest"))
        item, _ = match_target(target, items, statuses=open_)
        payload: dict[str, Any] = {}
        if when is not None:
            payload = {"when": when.astimezone(ZoneInfo("UTC")).isoformat(),
                       "keep_time": not _CLOCK_WORDS.search(moved.group("rest"))}
        return _action("RESCHEDULE", payload, item=item, target=target, unresolved=[] if when else ["when"])
    snoozed = SNOOZE.match(raw)
    if snoozed:
        rest = snoozed.group("rest")
        parsed = parse_task(rest, now=now, timezone_name=zone.key)
        until = _moment(parsed)
        if until is None and parsed.get("estimated_total_effort_minutes"):
            until = now + timedelta(minutes=int(parsed["estimated_total_effort_minutes"]))  # "отложи на час"
        target = _clean_target(parsed.get("title") or rest)
        item, _ = match_target(target, items, kinds=("TASK", "REMINDER"), statuses=open_)
        if item is None or until is None:
            return None  # "напомни про встречу с деканом завтра": a new reminder
        return _action("SNOOZE", {"until": until.astimezone(ZoneInfo("UTC")).isoformat()}, item=item, target=target)
    return None
