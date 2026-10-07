"""Deterministic RU/EN reading of recurring requests (schema v31, ADR 0034).

    «Каждый день в 9 утра напоминай принять витамин D»   → MEDICATION check-in
    «Решать по 20 задач матана каждый день»               → QUOTA check-in
    «Каждый вечер в 22:30 напоминай вынести мусор»         → reminder series
    «По будням утром напомни взять пропуск»                → reminder series, weekdays

Mirrored on the device by ``web/static/js/recurring.js`` and held to the same
fixture (``tests/fixtures/nl_recurring_cases.json``). A request is recognized only
when an explicit recurrence marker is present, so one-shot captures keep going to
``nlparse``. It never guesses a medication: the kind comes from the verb the user
used («принять/принимать/выпить», "take"); the name and the dose are their words.

Matching runs on a lower-cased copy of the same length; boundaries are explicit
(``(?<![0-9a-zа-я])``) because JavaScript's ``\\b``/``\\w`` do not know Cyrillic.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .nlparse import _PARTS, _hour

L = r"(?<![0-9a-zа-я])"
R = r"(?![0-9a-zа-я])"
_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_DAY_STEMS = [
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5), ("воскресен", 6),
    ("monday", 0), ("tuesday", 1), ("wednesday", 2), ("thursday", 3), ("friday", 4), ("saturday", 5), ("sunday", 6),
]
_DAY_WORD = (r"(?:понедельник[а-я]*|вторник[а-я]*|сред[а-я]*|четверг[а-я]*|пятниц[а-я]*|суббот[а-я]*|воскресен[а-я]*"
             r"|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)")
# (pattern, rule kind, implied day part). The first marker found decides the rule.
_MARKERS = [
    (r"каждый\s+будний\s+день|по\s+будн(?:ям|им\s+дням)|в\s+будни|on\s+weekdays|every\s+weekday|weekdays", "WEEKDAYS", None),
    (r"по\s+выходным|каждые\s+выходные|on\s+weekends|every\s+weekend", "WEEKENDS", None),
    (rf"(?:кажд(?:ый|ую|ое)\s+|по\s+|every\s+|on\s+){_DAY_WORD}(?:\s*(?:,|и|and)\s*{_DAY_WORD})*", "DAYS", None),
    (r"каждое\s+утро|every\s+morning", "DAILY", "morning"),
    (r"каждый\s+вечер|every\s+evening|every\s+night", "DAILY", "evening"),
    (r"каждый\s+день|ежедневно|every\s+day|daily", "DAILY", None),
    (r"через\s+день|every\s+other\s+day", "EVERY_OTHER_DAY", None),
    (r"каждую\s+неделю|раз\s+в\s+неделю|every\s+week|weekly|once\s+a\s+week", "WEEKLY", None),
    (r"в\s+день|per\s+day|a\s+day", "PER_DAY", None),
]
_REMIND = (rf"{L}(?:напоминай(?:те)?|напомни(?:те)?|напоминать|remind)(?:\s+(?:мне|me))?(?:\s+(?:to|о|об|про|что))?{R}"
           rf"|{L}не\s+забы(?:ть|вать){R}")
_TIME = rf"{L}(?:в|во|at)\s+(\d{{1,2}})(?:[:.](\d{{2}}))?\s*(утра|вечера|дня|ночи|am|pm)?{R}(?!\s*(?:мин|час|ч{R}|%))"
_TIME_AND = r"^\s*(?:и|and)\s+(?:в\s+|at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(утра|вечера|дня|ночи|am|pm)?(?![0-9a-zа-я])"
_PART_WORD = [
    (rf"{L}(?:утром|с\s+утра|in\s+the\s+morning){R}", "morning"),
    (rf"{L}(?:дн[её]м|in\s+the\s+afternoon){R}", "afternoon"),
    (rf"{L}(?:вечером|in\s+the\s+evening){R}", "evening"),
    (rf"{L}(?:перед\s+сном|на\s+ночь|before\s+bed){R}", "night"),
]
_MEDICATION_VERB = rf"{L}(?:принять|принимать|выпить|пить|take){R}\s+"
# English "take" is everyday language ("take the badge"): only with a medication word or a dose.
_EN_MEDICATION = rf"{L}(?:pills?|tablets?|capsules?|drops|vitamins?|medicines?|medications?|meds){R}"
_NOT_MEDICATION = r"^(?:душ|ванн|решени|участи|гост|экзамен|зач[её]т|звон|заказ|посылк|a\s+shower|a\s+bath|part|notes)"
_DOSE = (rf"{L}\d+(?:[.,]\d+)?\s*(?:мг|mg|мкг|mcg|мл|ml|ме|iu|ед){R}"
         rf"|{L}\d+\s*(?:таблетк[а-я]*|капсул[а-я]*|капел[а-я]*|капл[а-я]*|tablets?|pills?|drops?)(?:\s+(?:of)(?![0-9a-zа-я]))?")
_QUOTA = rf"(?:{L}по\s+)?{L}(\d{{1,6}})\s+([a-zа-я]{{2,30}})"
_SKIP_UNIT = r"^(?:раз|минут[а-я]*|мин|час[а-я]*|ч|утра|вечера|дня|ночи|am|pm|minutes?|hours?|times?|мг|mg|мл|ml)$"
_TRACK = rf"{L}(?:отмечать|отслеживать|трекать|привычк[а-я]*|track|habit){R}"


def _low(text: str) -> str:
    return text.lower().replace("ё", "е")


def _blank(chars: list[str], start: int, end: int) -> None:
    for i in range(max(0, start), min(end, len(chars))):
        chars[i] = " "


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;:—-!")
    while True:
        stripped = re.sub(r"^(?:и|and|чтобы|to|of)\s+", "", text, flags=re.IGNORECASE)
        stripped = re.sub(r"\s+(?:и|and|of)$", "", stripped, flags=re.IGNORECASE)
        if stripped == text:
            break
        text = stripped.strip(" ,.;:—-!")
    return text.strip(" ,.;:—-!")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _first_start(time_value: str, rule_kind: str, days: list[int], now: datetime, zone: ZoneInfo,
                 *, include_today: bool = False) -> str | None:
    hh, mm = (int(part) for part in time_value.split(":"))
    local_now = now.astimezone(zone).replace(tzinfo=None)
    allowed = {"WEEKDAYS": [0, 1, 2, 3, 4], "WEEKENDS": [5, 6], "DAYS": days}.get(rule_kind)
    for offset in range(0, 15):
        day = (local_now + timedelta(days=offset)).date()
        candidate = datetime(day.year, day.month, day.day, hh, mm)
        if candidate <= local_now and not (include_today and offset == 0):
            continue
        if allowed is not None and candidate.weekday() not in allowed:
            continue
        return candidate.strftime("%Y-%m-%dT%H:%M")
    return None


def _rule(rule_kind: str, days: list[int]) -> str:
    if rule_kind == "WEEKDAYS":
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    if rule_kind == "WEEKENDS":
        return "FREQ=WEEKLY;BYDAY=SA,SU"
    if rule_kind == "DAYS":
        return "FREQ=WEEKLY;BYDAY=" + ",".join(_WEEKDAYS[d] for d in sorted(set(days)))
    if rule_kind == "EVERY_OTHER_DAY":
        return "FREQ=DAILY;INTERVAL=2"
    if rule_kind == "WEEKLY":
        return "FREQ=WEEKLY"
    return "FREQ=DAILY"


def parse_recurring(text: str, *, now: datetime, timezone_name: str = "UTC") -> dict[str, object] | None:
    """A typed reading of a recurring request, or None when there is no recurrence marker."""
    raw = " ".join(str(text or "").split())
    if not raw or len(raw) > 300:
        return None
    low = _low(raw)
    if len(low) != len(raw):
        return None
    zone = ZoneInfo(timezone_name)
    marker = None
    for pattern, rule_kind, part in _MARKERS:
        found = re.search(rf"{L}(?:{pattern}){R}", low)
        if found:
            marker = (found, rule_kind, part)
            break
    if marker is None:
        return None
    found, rule_kind, implied_part = marker
    time_spans = [m.span() for m in re.finditer(_TIME, low)]
    per_day = re.search(rf"{L}(?:в\s+день|per\s+day|a\s+day){R}", low)
    quota = None
    for match in re.finditer(_QUOTA, low):
        if any(a <= match.start(1) < b for a, b in time_spans) or re.match(_SKIP_UNIT, match.group(2)):
            continue
        if re.search(_REMIND, match.group(2)):
            continue
        if match.group(0).startswith("по") or (per_day is not None and per_day.start() >= match.end()):
            quota = match
            break
    if rule_kind == "PER_DAY":
        if quota is None:
            return None  # «в день» alone is no recurrence («в день рождения»)
        rule_kind = "DAILY"
    days: list[int] = []
    masked = list(raw)
    if rule_kind == "DAYS":
        for word in re.findall(_DAY_WORD, found.group(0)):
            days.extend(index for stem, index in _DAY_STEMS if word.startswith(stem))
    elif rule_kind == "WEEKLY":
        # «каждую неделю в субботу»: the weekday names the day of the week.
        named = re.search(rf"{L}(?:в|во|on)\s+({_DAY_WORD}){R}", low)
        if named is not None:
            days.extend(index for stem, index in _DAY_STEMS if named.group(1).startswith(stem))
            rule_kind = "DAYS"
            _blank(masked, *named.span())
    _blank(masked, *found.span())
    if per_day is not None:
        _blank(masked, *per_day.span())
    times: list[str] = []
    first_time = re.search(_TIME, low)
    if first_time is not None:
        hour, minute, suffix = int(first_time.group(1)), int(first_time.group(2) or 0), first_time.group(3)
        if hour <= 23 and minute <= 59:
            if suffix is None and implied_part == "evening" and hour < 12:
                suffix = "вечера"
            elif suffix is None and implied_part == "morning":
                suffix = "утра"
            times.append(f"{_hour(hour, suffix, explicit=first_time.group(2) is not None or hour > 12):02d}:{minute:02d}")
            _blank(masked, *first_time.span())
            extra = re.match(_TIME_AND, low[first_time.end():])
            if extra is not None and int(extra.group(1)) <= 23 and int(extra.group(2) or 0) <= 59:
                h2, m2 = int(extra.group(1)), int(extra.group(2) or 0)
                times.append(f"{_hour(h2, extra.group(3), explicit=extra.group(2) is not None or h2 > 12):02d}:{m2:02d}")
                _blank(masked, first_time.end(), first_time.end() + extra.end())
    part = implied_part
    for pattern, name in _PART_WORD:
        hit = re.search(pattern, low)
        if hit is not None:
            _blank(masked, *hit.span())
            part = part or name
    if not times and part:
        anchor = _PARTS[part][2]
        times.append(f"{anchor.hour:02d}:{anchor.minute:02d}")
    for hit in re.finditer(_REMIND, low):
        _blank(masked, *hit.span())
    times = sorted(set(times))
    result: dict[str, object] = {"recurrence_rule": _rule(rule_kind, days), "times": times, "unresolved": []}
    if quota is not None:
        if quota.group(0).startswith("по"):
            _blank(masked, quota.start(), quota.start(1))
        title = _clean("".join(masked))
        if not title:
            return None
        result.update(kind="CHECKIN", checkin_kind="QUOTA", title=_cap(title),
                      target_quantity=int(quota.group(1)), unit=raw[quota.start(2):quota.end(2)],
                      remind=bool(times))
        result["dtstart_local"] = _first_start(times[0] if times else "00:00", rule_kind, days, now, zone,
                                               include_today=not times)
        return result
    rest = "".join(masked)
    rest_low = _low(rest)
    verb = re.search(_MEDICATION_VERB, rest_low)
    if verb is not None and rest_low[verb.start():verb.start() + 4] == "take" \
            and not (re.search(_EN_MEDICATION, rest_low[verb.end():]) or re.search(_DOSE, rest_low[verb.end():])):
        verb = None
    if verb is not None and not re.match(_NOT_MEDICATION, rest_low[verb.end():]):
        name_raw, name_low = rest[verb.end():], rest_low[verb.end():]
        dose = re.search(_DOSE, name_low)
        if dose is not None:
            result["dose_text"] = _clean(name_raw[dose.start():dose.end()])
            name_raw = name_raw[:dose.start()] + " " + name_raw[dose.end():]
        name = _clean(name_raw)
        if not name:
            return None
        result.update(kind="CHECKIN", checkin_kind="MEDICATION", title=_cap(name))
    elif re.search(_TRACK, rest_low):
        track = re.search(_TRACK, rest_low)
        title = _clean(rest[:track.start()] + " " + rest[track.end():])
        if not title:
            return None
        result.update(kind="CHECKIN", checkin_kind="ROUTINE", title=_cap(title))
    else:
        title = _clean(rest)
        if not title:
            return None
        result.update(kind="REMINDER_SERIES", title=_cap(title))
    if not times:
        result["unresolved"] = ["time"]
        result["dtstart_local"] = None
    else:
        result["dtstart_local"] = _first_start(times[0], rule_kind, days, now, zone)
    return result
