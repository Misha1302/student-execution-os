"""Localized texts of group notifications: a change is shown as a diff, not as a copy.

    Контрольная перенесена · Чт 10:30 → Пт 12:10
    Аудитория изменена · R201 → R301
    Контрольная отменена

The texts go through the existing reminder outbox (``reminder_messages``), so they
reach the in-app inbox and the phone exactly like reminders do.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_WEEKDAYS = {"ru": ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"), "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}

_EVENT_NOUN = {
    "ru": {"QUIZ": "Квиз", "TEST": "Тест", "CONTROL_WORK": "Контрольная", "COLLOQUIUM": "Коллоквиум", "EXAM": "Экзамен",
           "LECTURE": "Лекция", "SEMINAR": "Семинар", "PRACTICE": "Практика", "LAB": "Лабораторная",
           "CONSULTATION": "Консультация", "CLASS": "Занятие", "GROUP_MEETING": "Встреча группы", "OTHER": "Событие"},
    "en": {"QUIZ": "Quiz", "TEST": "Test", "CONTROL_WORK": "Control work", "COLLOQUIUM": "Colloquium", "EXAM": "Exam",
           "LECTURE": "Lecture", "SEMINAR": "Seminar", "PRACTICE": "Practice", "LAB": "Lab", "CONSULTATION": "Consultation",
           "CLASS": "Class", "GROUP_MEETING": "Group meeting", "OTHER": "Event"},
}

_TEXT = {
    "ru": {
        "moved": "{noun} {moved}", "room": "Аудитория изменена",
        "cancelled": "{noun} {cancelled}",
        "new_event": "Новое в группе: {noun}", "new_obligation": "Новый дедлайн в группе",
        "deadline_moved": "Дедлайн перенесён", "obligation_cancelled": "Дедлайн отменён",
        "announcement": "Объявление группы", "retracted": "Объявление отозвано",
        "critical": "Критично: ", "proposal": "Новое предложение в группе",
        "approved": "Ваше предложение опубликовано", "rejected": "Ваше предложение отклонено",
        "joined": "Вас приняли в группу", "updated": "{noun}: изменения",
    },
    "en": {
        "moved": "{noun} moved", "room": "Room changed",
        "cancelled": "{noun} cancelled",
        "new_event": "New in your group: {noun}", "new_obligation": "New group deadline",
        "deadline_moved": "Deadline moved", "obligation_cancelled": "Deadline cancelled",
        "announcement": "Group announcement", "retracted": "Announcement retracted",
        "critical": "Critical: ", "proposal": "New suggestion in your group",
        "approved": "Your suggestion was published", "rejected": "Your suggestion was declined",
        "joined": "You were admitted to the group", "updated": "{noun}: changed",
    },
}

# Grammatical gender of the Russian nouns above: «Квиз перенесён», «Контрольная
# перенесена», «Занятие перенесено».
_GENDER_RU = {"QUIZ": "m", "TEST": "m", "EXAM": "m", "COLLOQUIUM": "m", "SEMINAR": "m", "CLASS": "n", "OTHER": "n"}
_PARTICIPLE_RU = {"moved": {"m": "перенесён", "f": "перенесена", "n": "перенесено"},
                  "cancelled": {"m": "отменён", "f": "отменена", "n": "отменено"}}


def _headline(key: str, kind: str, locale: str) -> str:
    gender = _GENDER_RU.get(kind, "f")
    return _TEXT[locale][key].format(noun=_noun(kind, locale), moved=_PARTICIPLE_RU["moved"][gender],
                                     cancelled=_PARTICIPLE_RU["cancelled"][gender])


def when(value: datetime | str, timezone_name: str, locale: str) -> str:
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    local = at.astimezone(ZoneInfo(timezone_name))
    day = _WEEKDAYS[locale][local.weekday()]
    date = local.strftime("%d.%m") if locale == "ru" else local.strftime("%b %d")
    return f"{day} {date} {local.strftime('%H:%M')}"


def _noun(kind: str | None, locale: str) -> str:
    return _EVENT_NOUN[locale].get(kind or "OTHER", _EVENT_NOUN[locale]["OTHER"])


def _critical(prefix: bool, locale: str) -> str:
    return _TEXT[locale]["critical"] if prefix else ""


def event_changed(event: dict[str, Any], changes: dict[str, Any], *, locale: str, timezone_name: str,
                  critical: bool) -> dict[str, str] | None:
    """A reschedule / room change of one event as a diff; None when nothing the member acts on changed."""
    t = _TEXT[locale]
    kind = event["event_kind"]
    lines = []
    head = None
    if "starts_at" in changes or "ends_at" in changes:
        old = changes.get("starts_at", [event["starts_at"], event["starts_at"]])[0]
        new = changes.get("starts_at", [event["starts_at"], event["starts_at"]])[1]
        head = _headline("moved", kind, locale)
        lines.append(f"{when(old, timezone_name, locale)} → {when(new, timezone_name, locale)}")
    if "location" in changes:
        old, new = changes["location"]
        head = head or t["room"]
        lines.append(f"{old or '—'} → {new or '—'}")
    if head is None:
        return None
    return {"title": _critical(critical, locale) + head, "body": f"«{event['title']}» · " + " · ".join(lines)
            if locale == "ru" else f"“{event['title']}” · " + " · ".join(lines)}


def event_cancelled(event: dict[str, Any], *, locale: str, timezone_name: str, critical: bool) -> dict[str, str]:
    head = _headline("cancelled", event["event_kind"], locale)
    quoted = f"«{event['title']}»" if locale == "ru" else f"“{event['title']}”"
    return {"title": _critical(critical, locale) + head, "body": f"{quoted} · {when(event['starts_at'], timezone_name, locale)}"}


def event_published(event: dict[str, Any], *, locale: str, timezone_name: str, critical: bool) -> dict[str, str]:
    head = _TEXT[locale]["new_event"].format(noun=_noun(event["event_kind"], locale))
    quoted = f"«{event['title']}»" if locale == "ru" else f"“{event['title']}”"
    return {"title": _critical(critical, locale) + head, "body": f"{quoted} · {when(event['starts_at'], timezone_name, locale)}"}


def obligation_published(item: dict[str, Any], *, locale: str, timezone_name: str, critical: bool) -> dict[str, str]:
    quoted = f"«{item['title']}»" if locale == "ru" else f"“{item['title']}”"
    return {"title": _critical(critical, locale) + _TEXT[locale]["new_obligation"],
            "body": f"{quoted} · {when(item['deadline'], timezone_name, locale)}"}


def obligation_changed(item: dict[str, Any], changes: dict[str, Any], *, locale: str, timezone_name: str,
                       critical: bool) -> dict[str, str] | None:
    if "deadline" not in changes:
        return None
    old, new = changes["deadline"]
    quoted = f"«{item['title']}»" if locale == "ru" else f"“{item['title']}”"
    return {"title": _critical(critical, locale) + _TEXT[locale]["deadline_moved"],
            "body": f"{quoted} · {when(old, timezone_name, locale)} → {when(new, timezone_name, locale)}"}


def obligation_cancelled(item: dict[str, Any], *, locale: str, critical: bool) -> dict[str, str]:
    quoted = f"«{item['title']}»" if locale == "ru" else f"“{item['title']}”"
    return {"title": _critical(critical, locale) + _TEXT[locale]["obligation_cancelled"], "body": quoted}


def announcement(item: dict[str, Any], *, locale: str) -> dict[str, str]:
    body = item["body"] if len(item["body"]) <= 180 else item["body"][:177] + "…"
    return {"title": f"{_TEXT[locale]['announcement']}: {item['title']}", "body": body}


def proposal_waiting(proposal_title: str, group_name: str, *, locale: str) -> dict[str, str]:
    return {"title": _TEXT[locale]["proposal"], "body": f"{group_name} · {proposal_title}"}


def proposal_reviewed(approved: bool, proposal_title: str, *, locale: str) -> dict[str, str]:
    return {"title": _TEXT[locale]["approved" if approved else "rejected"], "body": proposal_title}


def membership_approved(group_name: str, *, locale: str) -> dict[str, str]:
    return {"title": _TEXT[locale]["joined"], "body": group_name}
