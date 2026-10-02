"""Server-side authority over *which* existing item an Assistant action addresses.

A model may pick a target, but the server decides whether that pick is
sufficiently unique. The decision uses only deterministic evidence:

* the bounded, authorized candidate set the model was shown (the account's
  context items and the previous Assistant turn) — an identifier outside it is
  rejected like an invented one;
* the words the user actually said (title-token overlap with Russian/English
  inflection tolerance; the model's ``target_text`` counts only when every one of
  its words appears in the user's text);
* explicit kind words ("напоминание", "задача"…) and explicit dates/times the user
  mentioned for the item (a RESCHEDULE's destination time is not evidence);
* the previous Assistant turn ("перенеси её ещё на час").

Outcome: one materially unique candidate → continue; several materially plausible
candidates → the action becomes an unresolved target with those candidates for the
user to pick; a pick outside the authorized set → rejected. Model confidence is
never used as evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_WORD = re.compile(r"[0-9a-zа-я]+")
_STOP = frozenset("""
с со на в во к ко по и о об у за до от для из под над при про мне мой моя моё мое мои меня мою моей мной
это эту этот эта ту тот та его её ее их ей ему им ним ней нее неё него её же ли бы а но или
the a an to with at on in of my for and or by from
""".split())
# Words that name the kind of item, not its title.
_KIND_HINTS = (
    ("напомин", "REMINDER"), ("reminder", "REMINDER"),
    ("задач", "TASK"), ("task", "TASK"),
    ("событи", "EVENT"), ("event", "EVENT"),
)
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_WEEKDAYS = (
    ("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5),
    ("воскресень", 6), ("monday", 0), ("tuesday", 1), ("wednesday", 2), ("thursday", 3), ("friday", 4),
    ("saturday", 5), ("sunday", 6),
)
MAX_CANDIDATES = 5


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(str(text).casefold().replace("ё", "е")) if w not in _STOP and len(w) > 1]


def same_word(left: str, right: str) -> bool:
    """Equal, or the same stem under common Russian/English inflection («встречу» ~ «встреча»)."""
    if left == right:
        return True
    if left.isdigit() or right.isdigit():
        return False
    shortest = min(len(left), len(right))
    if shortest < 3:
        return False
    common = 0
    for a, b in zip(left, right):
        if a != b:
            break
        common += 1
    return common >= max(3, shortest - 2)


def title_score(title: str, said: list[str]) -> tuple[float, int]:
    """(share of the title's words the user said, how many of them): the second part
    prefers the more specific title when several are fully named («…по курсовой»)."""
    words = tokens(title)
    if not words:
        return 0.0, 0
    matched = sum(1 for word in words if any(same_word(word, s) for s in said))
    return matched / len(words), matched


def mentioned_dates(text: str, now: datetime, zone: ZoneInfo) -> set[date]:
    today = now.astimezone(zone).date()
    found: set[date] = set()
    lowered = str(text).casefold().replace("ё", "е")
    words = _WORD.findall(lowered)
    for word in words:
        if word in {"сегодня", "today"}:
            found.add(today)
        elif word in {"завтра", "tomorrow"}:
            found.add(today + timedelta(days=1))
        elif word == "послезавтра":
            found.add(today + timedelta(days=2))
        elif word in {"вчера", "yesterday"}:
            found.add(today - timedelta(days=1))
        else:
            for stem, weekday in _WEEKDAYS:
                if word.startswith(stem):
                    found.add(today + timedelta(days=(weekday - today.weekday()) % 7))
    for day, month in re.findall(r"\b(\d{1,2})[./](\d{1,2})\b", lowered):
        try:
            found.add(date(today.year, int(month), int(day)))
        except ValueError:
            pass
    for day, word in re.findall(r"\b(\d{1,2})\s+([а-я]+)", lowered):
        month = next((number for stem, number in _MONTHS.items()
                      if word.startswith(stem) and (stem != "ма" or word in {"мая", "май"})), None)
        if month:
            try:
                found.add(date(today.year, month, int(day)))
            except ValueError:
                pass
    return found


def mentioned_times(text: str) -> set[time]:
    found: set[time] = set()
    for hour, minute in re.findall(r"\b(\d{1,2})[:.](\d{2})\b", str(text)):
        if int(hour) < 24 and int(minute) < 60:
            found.add(time(int(hour), int(minute)))
    for hour in re.findall(r"\b(?:в|at)\s+(\d{1,2})\b(?![:.]\d)", str(text).casefold()):
        if int(hour) < 24:
            found.add(time(int(hour), 0))
    return found


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str
    title: str
    version: int
    moment: datetime | None

    def payload(self) -> dict[str, object]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "version": self.version,
                **({"when": self.moment.isoformat()} if self.moment else {})}


def candidates_from_context(context: dict[str, object]) -> list[Candidate]:
    result: list[Candidate] = []
    for item in [*(context.get("obligations") or []), *(context.get("reminders") or [])]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        raw = item.get("starts_at") or item.get("remind_at") or item.get("due")
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
        except ValueError:
            moment = None
        result.append(Candidate(str(item["id"]), str(item.get("kind") or ""), str(item.get("title") or ""),
                                int(item.get("version") or 0), moment))
    return result


def session_target_ids(context: dict[str, object]) -> set[str]:
    session = context.get("assistant_session")
    previous = session.get("previous_actions") if isinstance(session, dict) else None
    ids: set[str] = set()
    for action in previous if isinstance(previous, list) else []:
        payload = action.get("payload") if isinstance(action, dict) else None
        if isinstance(payload, dict):
            ids.update(str(payload[key]) for key in ("obligation_id", "reminder_id") if payload.get(key))
    return ids


@dataclass(frozen=True)
class Verdict:
    decision: str                      # CONTINUE | AMBIGUOUS | OUT_OF_SCOPE
    reason: str
    candidates: tuple[Candidate, ...] = ()


def judge(*, chosen_id: str, allowed_kinds: set[str], text: str, target_text: object,
          context: dict[str, object], now: datetime, zone: ZoneInfo,
          destination: datetime | None = None) -> Verdict:
    """Decide whether the model's pick is the one materially unique candidate."""
    pool = [c for c in candidates_from_context(context) if c.kind in allowed_kinds]
    session = session_target_ids(context)
    chosen = next((c for c in pool if c.id == chosen_id), None)
    if chosen is None and chosen_id not in session:
        return Verdict("OUT_OF_SCOPE", "TARGET_NOT_IN_AUTHORIZED_CONTEXT")

    said = tokens(text)
    phrase = tokens(target_text) if isinstance(target_text, str) else []
    if phrase and all(any(same_word(word, s) for s in said) for word in phrase):
        said = phrase  # the model's span of the user's own words, e.g. one of two items in one sentence

    scored = [(title_score(c.title, said), c) for c in pool]
    best = max((score for score, _ in scored), default=(0.0, 0))
    if best[0] < 0.5:
        # Nothing the user said names an item ("перенеси её"): the pick rests on the
        # conversation, which the server cannot contradict.
        return Verdict("CONTINUE", "NO_LEXICAL_EVIDENCE")
    tied = [c for score, c in scored if score == best]

    hinted = {kind for stem, kind in _KIND_HINTS if any(word.startswith(stem) for word in tokens(text))}
    if hinted and any(c.kind in hinted for c in tied):
        tied = [c for c in tied if c.kind in hinted]

    if len(tied) > 1:
        local_destination = destination.astimezone(zone) if destination else None
        dates = {d for d in mentioned_dates(text, now, zone)
                 if local_destination is None or d != local_destination.date()}
        times = {t for t in mentioned_times(text)
                 if local_destination is None or t != local_destination.time().replace(second=0, microsecond=0)}
        narrowed = tied
        if dates:
            by_date = [c for c in narrowed if c.moment and c.moment.astimezone(zone).date() in dates]
            narrowed = by_date or narrowed
        if times and len(narrowed) > 1:
            by_time = [c for c in narrowed
                       if c.moment and c.moment.astimezone(zone).time().replace(second=0, microsecond=0) in times]
            narrowed = by_time or narrowed
        tied = narrowed

    if len(tied) > 1 and chosen_id in session and any(c.id == chosen_id for c in tied):
        return Verdict("CONTINUE", "SESSION_CONTINUATION")
    if len(tied) == 1 and tied[0].id == chosen_id:
        return Verdict("CONTINUE", "UNIQUE")
    if len(tied) == 1 and chosen_id in session:
        return Verdict("CONTINUE", "SESSION_CONTINUATION")
    options = list(tied)
    if chosen is not None and chosen not in options:
        options.append(chosen)  # the model's pick stays selectable, but the user decides
    return Verdict("AMBIGUOUS", "MULTIPLE_PLAUSIBLE" if len(tied) > 1 else "PICK_CONTRADICTS_TEXT",
                   tuple(options[:MAX_CANDIDATES]))
