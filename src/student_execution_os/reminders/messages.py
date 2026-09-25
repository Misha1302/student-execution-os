"""Localized reminder texts and the actions each prompt offers.

Android shows at most three action buttons; tapping the notification body always
opens the related context, so OPEN is only listed where it adds a distinct meaning.
Actions marked ``background`` can be executed by the device without opening the app.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .policy import Stage, TaskFacts

ACTIONS = {
    "START": {"background": True},
    "SNOOZE_30": {"background": True, "minutes": 30},
    "SNOOZE_60": {"background": True, "minutes": 60},
    "DONE": {"background": True},
    "RESCHEDULE": {"background": False},
    "REPLAN": {"background": False},
    "OPEN": {"background": False},
}

_STAGE_ACTIONS = {
    Stage.START_SOON: ("START", "SNOOZE_30", "DONE"),
    Stage.START_NOW: ("START", "SNOOZE_60", "RESCHEDULE"),
    Stage.CHECK_IN: ("DONE", "SNOOZE_60", "OPEN"),
    Stage.DONE_CHECK: ("DONE", "OPEN"),
    Stage.DEADLINE_24H: ("START", "SNOOZE_60", "RESCHEDULE"),
    Stage.DEADLINE_2H: ("START", "DONE", "RESCHEDULE"),
    Stage.RISK_UP: ("START", "REPLAN", "RESCHEDULE"),
    Stage.OVERDUE: ("DONE", "RESCHEDULE", "OPEN"),
    Stage.GROUP: ("OPEN", "SNOOZE_60"),
    Stage.REMINDER: ("START", "DONE", "SNOOZE_30"),
}

_LABELS = {
    "ru": {"START": "Начать", "SNOOZE_30": "Через 30 мин", "SNOOZE_60": "Через час", "DONE": "Готово",
           "RESCHEDULE": "Перенести", "REPLAN": "План", "OPEN": "Открыть"},
    "en": {"START": "Start", "SNOOZE_30": "In 30 min", "SNOOZE_60": "In 1 hour", "DONE": "Done",
           "RESCHEDULE": "Reschedule", "REPLAN": "Plan", "OPEN": "Open"},
}

_TEXT = {
    "ru": {
        Stage.START_SOON: ("Скоро пора начинать", "«{title}»: нужно {effort}, начать до {lss}"),
        Stage.START_NOW: ("Пора начать «{title}»", "Нужно ещё {effort}{due_clause}"),
        "START_NOW_REPEAT": ("«{title}» всё ещё не начато", "Начните сейчас или перенесите{due_clause}"),
        Stage.CHECK_IN: ("Как продвигается «{title}»?", "Осталось {effort}. Отметьте прогресс или завершите"),
        Stage.DONE_CHECK: ("«{title}» готово?", "Оставшееся время закончилось — отметьте выполненной"),
        Stage.DEADLINE_24H: ("Дедлайн {due_rel}", "«{title}» ещё не начато, нужно {effort}"),
        Stage.DEADLINE_2H: ("До дедлайна меньше 2 часов", "«{title}»: осталось {effort}, срок {due}"),
        Stage.RISK_UP: ("«{title}» под угрозой", "Времени до срока почти не осталось — начните или перепланируйте"),
        Stage.OVERDUE: ("Срок «{title}» прошёл", "Отметьте выполненной или назначьте новый срок"),
        Stage.GROUP: ("{count} задачи требуют внимания", "{titles}"),
        Stage.REMINDER: ("Напоминаю: «{title}»", "{reminder_body}"),
    },
    "en": {
        Stage.START_SOON: ("Time to start soon", "“{title}”: needs {effort}, start by {lss}"),
        Stage.START_NOW: ("Time to start “{title}”", "Needs {effort} more{due_clause}"),
        "START_NOW_REPEAT": ("“{title}” isn't started yet", "Start now or reschedule{due_clause}"),
        Stage.CHECK_IN: ("How is “{title}” going?", "{effort} left. Log progress or mark it done"),
        Stage.DONE_CHECK: ("Is “{title}” done?", "No remaining time is left — mark it complete"),
        Stage.DEADLINE_24H: ("Due {due_rel}", "“{title}” isn't started, needs {effort}"),
        Stage.DEADLINE_2H: ("Less than 2 hours to the deadline", "“{title}”: {effort} left, due {due}"),
        Stage.RISK_UP: ("“{title}” is at risk", "Little time is left before it's due — start or replan"),
        Stage.OVERDUE: ("“{title}” is past due", "Mark it done or set a new deadline"),
        Stage.GROUP: ("{count} tasks need attention", "{titles}"),
        Stage.REMINDER: ("Reminder: “{title}”", "{reminder_body}"),
    },
}

# Texts the device shows after a notification button was handled in the background.
FOLLOW_UP = {
    "ru": {"started": "В работе: «{title}»", "started_body": "Отметьте, когда закончите",
           "done": "Готово", "snoozed": "Напомню в {time}", "completed": "«{title}» выполнено",
           "failed": "Не удалось отправить — откройте приложение", "queued": "Отправлю, когда появится сеть"},
    "en": {"started": "In progress: “{title}”", "started_body": "Mark it done when you finish",
           "done": "Done", "snoozed": "I'll remind you at {time}", "completed": "“{title}” is done",
           "failed": "Couldn't send — open the app", "queued": "Will send when you're back online"},
}


def _effort(minutes: int | None, locale: str) -> str:
    if not minutes:
        return "немного времени" if locale == "ru" else "a little time"
    hours, mins = divmod(int(minutes), 60)
    if locale == "ru":
        return " ".join(p for p in (f"{hours} ч" if hours else "", f"{mins} мин" if mins else "") if p)
    return " ".join(p for p in (f"{hours} h" if hours else "", f"{mins} min" if mins else "") if p)


def _when(at: datetime | None, now: datetime, zone: ZoneInfo, locale: str) -> str:
    if at is None:
        return ""
    local, today = at.astimezone(zone), now.astimezone(zone)
    clock = local.strftime("%H:%M")
    days = (local.date() - today.date()).days
    if days == 0:
        return f"сегодня в {clock}" if locale == "ru" else f"today at {clock}"
    if days == 1:
        return f"завтра в {clock}" if locale == "ru" else f"tomorrow at {clock}"
    return local.strftime("%d.%m %H:%M") if locale == "ru" else local.strftime("%b %d, %H:%M")


def compose(stage: Stage, facts: list[TaskFacts], *, repeat: bool, now: datetime, timezone_name: str, locale: str) -> dict:
    zone = ZoneInfo(timezone_name)
    labels = _LABELS[locale]
    actions = [{"id": action_id, "label": labels[action_id], **ACTIONS[action_id]} for action_id in _STAGE_ACTIONS[stage]]
    if stage is Stage.GROUP:
        title_t, body_t = _TEXT[locale][Stage.GROUP]
        count = len(facts)
        if locale == "ru" and count >= 5:
            title_t = "{count} задач требуют внимания"
        return {
            "title": title_t.format(count=count),
            "body": " · ".join(item.title for item in facts[:4]) + (" …" if count > 4 else ""),
            "deep_link": "/today",
            "actions": actions,
        }
    item = facts[0]
    key = "START_NOW_REPEAT" if stage is Stage.START_NOW and repeat else stage
    title_t, body_t = _TEXT[locale][key]
    due = _when(item.due_at, now, zone, locale)
    due_clause = ""
    if due:
        due_clause = f", срок {due}" if locale == "ru" else f", due {due}"
    reminder_body = f"Срок {due}" if locale == "ru" and due else f"Due {due}" if due else (
        "Вы просили напомнить" if locale == "ru" else "You asked me to remind you")
    values = {
        "reminder_body": reminder_body,
        "title": item.title if len(item.title) <= 60 else item.title[:57] + "…",
        "effort": _effort(item.remaining_minutes, locale),
        "lss": _when(item.latest_safe_start, now, zone, locale),
        "due": due,
        "due_rel": due,
        "due_clause": due_clause,
    }
    return {"title": title_t.format(**values), "body": body_t.format(**values),
            "deep_link": f"/task/{item.task_id}", "actions": actions}
