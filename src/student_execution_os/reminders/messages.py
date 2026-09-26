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
    "SNOOZE_10": {"background": True, "minutes": 10},
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
    Stage.ESCALATION: ("START", "SNOOZE_30", "RESCHEDULE"),
}

_LABELS = {
    "ru": {"START": "Начать", "SNOOZE_10": "Через 10 мин", "SNOOZE_30": "Через 30 мин", "SNOOZE_60": "Через час", "DONE": "Готово",
           "RESCHEDULE": "Перенести", "REPLAN": "План", "OPEN": "Открыть"},
    "en": {"START": "Start", "SNOOZE_10": "In 10 min", "SNOOZE_30": "In 30 min", "SNOOZE_60": "In 1 hour", "DONE": "Done",
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
        Stage.ESCALATION: ("До срока «{title}» — {left}", "{progress}. Срок {due}"),
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
        Stage.ESCALATION: ("“{title}” is due in {left}", "{progress}. Due {due}"),
    },
}

# Texts the device shows after a notification button was handled in the background.
FOLLOW_UP = {
    "ru": {"started": "В работе: «{title}»", "started_body": "Отметьте, когда закончите",
           "done": "Готово", "snoozed": "Напомню в {time}", "completed": "«{title}» выполнено",
           "failed": "Не удалось отправить — откройте приложение", "queued": "Отправлю, когда появится сеть",
           "alarm_up": "Я встал", "alarm_done": "Выключить", "alarm_snooze": "Отложить на 10 мин",
           "awake_title": "Вы не уснули?", "awake_body": "Нажмите «Не сплю», иначе через 10 минут будильник зазвонит снова",
           "awake_ok": "Не сплю", "alarm_missed": "Будильник пропущен: «{title}»"},
    "en": {"started": "In progress: “{title}”", "started_body": "Mark it done when you finish",
           "done": "Done", "snoozed": "I'll remind you at {time}", "completed": "“{title}” is done",
           "failed": "Couldn't send — open the app", "queued": "Will send when you're back online",
           "alarm_up": "I'm up", "alarm_done": "Turn off", "alarm_snooze": "Snooze 10 min",
           "awake_title": "Still awake?", "awake_body": "Tap “I'm awake”, or the alarm rings again in 10 minutes",
           "awake_ok": "I'm awake", "alarm_missed": "Missed alarm: “{title}”"},
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
    if item.kind == "EVENT":
        return _event_reminder(item, labels, now=now, zone=zone, locale=locale)
    started = item.started_at is not None or item.last_progress_at is not None
    if stage is Stage.ESCALATION and started:
        # Already in progress: finishing is the useful button, not "start".
        actions = [{"id": a, "label": labels[a], **ACTIONS[a]} for a in ("DONE", "SNOOZE_30", "RESCHEDULE")]
    key = "START_NOW_REPEAT" if stage is Stage.START_NOW and repeat else stage
    title_t, body_t = _TEXT[locale][key]
    due = _when(item.due_at, now, zone, locale)
    due_clause = ""
    if due:
        due_clause = f", срок {due}" if locale == "ru" else f", due {due}"
    reminder_body = f"Срок {due}" if locale == "ru" and due else f"Due {due}" if due else (
        "Вы просили напомнить" if locale == "ru" else "You asked me to remind you")
    left_minutes = max(1, round(((item.due_at or now) - now).total_seconds() / 60))
    if locale == "ru":
        progress = f"В работе, осталось {_effort(item.remaining_minutes, locale)}" if started else \
            f"Ещё не начато, нужно {_effort(item.remaining_minutes, locale)}"
    else:
        progress = f"In progress, {_effort(item.remaining_minutes, locale)} left" if started else \
            f"Not started, needs {_effort(item.remaining_minutes, locale)}"
    values = {
        "left": _effort(left_minutes, locale) if left_minutes < 48 * 60 else ("2 дня" if locale == "ru" else "2 days"),
        "progress": progress,
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


def _event_reminder(item: TaskFacts, labels: dict[str, str], *, now: datetime, zone: ZoneInfo, locale: str) -> dict:
    """"Скоро: «Занятие по программированию»" — 21:00–22:00. Opening the app is the only action."""
    starts = item.target_at
    title = item.title if len(item.title) <= 60 else item.title[:57] + "…"
    span = ""
    if starts is not None:
        span = starts.astimezone(zone).strftime("%H:%M")
        if item.ends_at is not None:
            span += "–" + item.ends_at.astimezone(zone).strftime("%H:%M")
    minutes = None if starts is None else max(0, round((starts - now).total_seconds() / 60))
    if locale == "ru":
        head = f"Скоро: «{title}»" if minutes else f"Начинается: «{title}»"
        body = f"{_when(starts, now, zone, locale)}" + (f" ({span})" if span else "") + (f", через {minutes} мин" if minutes else "")
    else:
        head = f"Coming up: “{title}”" if minutes else f"Starting now: “{title}”"
        body = f"{_when(starts, now, zone, locale)}" + (f" ({span})" if span else "") + (f", in {minutes} min" if minutes else "")
    return {"title": head, "body": body, "deep_link": "/today",
            "actions": [{"id": "OPEN", "label": labels["OPEN"], **ACTIONS["OPEN"]}]}


def test_message(locale: str) -> dict:
    """The diagnostic notification sent from Settings → Notifications."""
    if locale == "ru":
        return {"title": "Проверка уведомлений", "body": "Если вы видите это на телефоне — напоминания дойдут.",
                "deep_link": "/settings", "actions": []}
    return {"title": "Notification check", "body": "If you can see this on your phone, reminders will reach you.",
            "deep_link": "/settings", "actions": []}


def compose_standalone(reminder: dict, *, now: datetime, timezone_name: str, locale: str) -> dict:
    """"Купить хлеб" — the user's own words are the notification; Done / later."""
    zone = ZoneInfo(timezone_name)
    labels = _LABELS[locale]
    title = reminder["title"] if len(reminder["title"]) <= 80 else reminder["title"][:77] + "…"
    at = datetime.fromisoformat(reminder["remind_at"])
    late = (now - at).total_seconds() > 300
    if reminder.get("note"):
        body = reminder["note"]
    elif locale == "ru":
        body = f"Вы просили напомнить {_when(at, now, zone, locale)}" if late else "Вы просили напомнить"
    else:
        body = f"You asked to be reminded {_when(at, now, zone, locale)}" if late else "You asked me to remind you"
    ids = ("DONE", "SNOOZE_10", "SNOOZE_60")
    link = f"/task/{reminder['obligation_id']}" if reminder.get("obligation_id") else f"/reminder/{reminder['id']}"
    return {"title": title, "body": body, "deep_link": link,
            "actions": [{"id": action_id, "label": labels[action_id], **ACTIONS[action_id]} for action_id in ids]}
