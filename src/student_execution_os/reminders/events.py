"""The reminder moment of an event: always "start minus the user's lead".

One owner for that rule, used by every writer that can change an event: the user's own
commands (``sync/commands.py``) and source refreshes (``recurrence/source.py``: academic
feeds, group schedules). An explicit reminder the user asked for is never erased by
automation; it is re-timed to the event's current start, suppressed while the event is
cancelled and restored with the event. Messages already queued for the old moment are
cancelled so the student is never told a wrong time.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from student_execution_os.domain.model import LifecycleStatus
from student_execution_os.persistence import extras
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

_OPEN = {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}


def event_reminder_moment(starts_at: datetime, status: LifecycleStatus, lead: int | None,
                          now: datetime) -> datetime | None:
    if lead is None or status not in _OPEN:
        return None
    remind = starts_at - timedelta(minutes=lead)
    if remind <= now:
        # Too late for the heads-up but still before the start: remind right away.
        return now + timedelta(minutes=1) if starts_at > now + timedelta(minutes=1) else None
    return remind


def sync_event_reminder(repo: SQLiteCanonicalRepository, account_id: str, event_id: str, now: datetime,
                        *, by_user: bool, lead: int | None = None, lead_given: bool = False) -> None:
    """Recompute the event's reminder moment after any change to it.

    ``by_user`` marks a change the user made (it counts as interacting with the item);
    a source change only re-times the reminder and drops messages about the old moment.
    """
    from .store import ReminderStore

    if lead_given:
        extras.set_event_lead(repo, account_id, event_id, lead)
    else:
        lead = extras.event_lead(repo, account_id, event_id)
        if lead is None:
            return  # the user never asked to be reminded of this event
    event = repo.get_event(account_id, event_id)
    moment = event_reminder_moment(event.interval.starts_at, event.obligation.lifecycle_status, lead, now)
    store = ReminderStore(repo)
    if by_user:
        store.set_remind_at(account_id, event_id, moment, now)
    else:
        store.retime(account_id, event_id, moment, now, reason="SOURCE_CHANGED")
