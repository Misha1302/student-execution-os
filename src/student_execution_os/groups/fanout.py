"""What a group change means for each member's own layer.

A shared fact changes once; every member's personal consequences are recomputed here,
in the same transaction, through the existing owners:

* a preparation / follow-up task that still tracks the old moment gets the new
  deadline (canonical ``update_task``); one the user re-dated themselves is only
  flagged, never overwritten;
* the "remind me N minutes before" moment moves (``reminder_states.remind_at``);
* an alarm for the event moves or is cancelled (standalone ``reminders``);
* members are told what changed (``reminder_messages``, the reminder outbox).

Nothing here deletes personal work: a cancelled assessment leaves the preparation
task in place and only flags it so the app can ask.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.domain.model import ActorCategory, CutoffState, HardCutoff, LifecycleStatus
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt

from .model import (
    ASSESSMENT_KINDS,
    Criticality,
    EventKind,
    Group,
    NotificationBehavior,
    SharedKind,
    UserSharedEventState,
)
from .repository import SQLiteGroupRepository

OPEN = (LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT)
EVENT_KEY = "shared-event:"
OBLIGATION_KEY = "shared-obligation:"


def reminder_key(kind: SharedKind, entity_id: str) -> str:
    return (EVENT_KEY if kind is SharedKind.SHARED_EVENT else OBLIGATION_KEY) + entity_id


def alarm_id(account_id: str, event_id: str) -> str:
    return "galarm-" + hashlib.sha256(f"{account_id}|{event_id}".encode()).hexdigest()[:32]


def lead_moment(start: datetime, lead: int | None, now: datetime) -> datetime | None:
    """``start - lead``; too late for the heads-up but still before the start → one minute from now."""
    if lead is None:
        return None
    moment = start - timedelta(minutes=lead)
    if moment <= now:
        return now + timedelta(minutes=1) if start > now + timedelta(minutes=1) else None
    return moment


class Fanout:
    def __init__(self, repo: SQLiteCanonicalRepository, groups: SQLiteGroupRepository, now: datetime) -> None:
        self.repo = repo
        self.groups = groups
        self.now = now

    # ---- per-account primitives --------------------------------------------------------

    def move_task(self, account_id: str, task_id: str | None, old: datetime, new: datetime) -> bool | None:
        """Move a linked open task's deadline if it still equals ``old``.

        True: moved. False: the user re-dated it (flag it). None: nothing to do.
        """
        if not task_id:
            return None
        try:
            task = self.repo.get_task(account_id, task_id)
        except EntityNotFound:
            return None
        if task.obligation.lifecycle_status not in OPEN:
            return None
        cutoff = task.actual_cutoff
        if cutoff.state is CutoffState.KNOWN and cutoff.at == old:
            self.repo.update_task(account_id=account_id, obligation_id=task_id, expected_version=task.obligation.version,
                                  actor=ActorCategory.SYSTEM, actual_cutoff=HardCutoff.known(new, cutoff.boundary))
            return True
        return False

    def task_is_open(self, account_id: str, task_id: str | None) -> bool:
        if not task_id:
            return False
        try:
            return self.repo.get_task(account_id, task_id).obligation.lifecycle_status in OPEN
        except EntityNotFound:
            return False

    def set_reminder(self, account_id: str, key: str, moment: datetime | None) -> None:
        from student_execution_os.reminders.store import ReminderStore
        ReminderStore(self.repo).set_remind_at(account_id, key, moment, self.now)

    def set_alarm(self, state: UserSharedEventState, title: str, start: datetime | None) -> str | None:
        """Keep the member's alarm for an event at ``start - lead``; cancel it when there is none."""
        from student_execution_os.reminders.standalone import SQLiteReminderRepository
        from student_execution_os.reminders.store import ReminderStore
        reminders = SQLiteReminderRepository(self.repo)
        rid = state.alarm_reminder_id or alarm_id(state.account_id, state.shared_event_id)
        moment = None if start is None else lead_moment(start, state.alarm_before_minutes, self.now)
        exists = reminders.exists(state.account_id, rid)
        if moment is None:
            if exists and reminders.get(state.account_id, rid)["status"] in ("SCHEDULED", "FIRED"):
                reminders.cancel_pending_messages(state.account_id, rid, "CANCELLED")
                reminders.cancel(state.account_id, rid, self.now)
                ReminderStore(self.repo).signal_alarm_sync(state.account_id, self.now)
            return rid if exists else None
        if exists:
            reminders.cancel_pending_messages(state.account_id, rid, "RESCHEDULED")
            reminders.update(state.account_id, rid, {"remind_at": moment, "title": title[:300]}, self.now)
        elif not reminders.is_deleted(state.account_id, rid):
            reminders.create(state.account_id, rid, title=title[:300], remind_at=moment, delivery="ALARM", note=None,
                             wake_check=False, raise_volume=False, obligation_id=None,
                             actor=ActorCategory.USER_UI.value, now=self.now)
        else:
            return None
        ReminderStore(self.repo).signal_alarm_sync(state.account_id, self.now)
        return rid

    # ---- event / obligation consequences ------------------------------------------------

    def event_moved(self, event: dict[str, Any], old_start: datetime, new_start: datetime) -> None:
        for state in self.groups.event_states_for_event(event["id"]):
            moved = self.move_task(state.account_id, state.preparation_task_id, old_start, new_start)
            stale = moved is False
            if state.remind_before_minutes is not None:
                self.set_reminder(state.account_id, reminder_key(SharedKind.SHARED_EVENT, event["id"]),
                                  lead_moment(new_start, state.remind_before_minutes, self.now))
            alarm = state.alarm_reminder_id
            if state.alarm_before_minutes is not None:
                alarm = self.set_alarm(state, event["title"], new_start)
            if stale != state.preparation_deadline_stale or alarm != state.alarm_reminder_id:
                self.groups.save_event_state(replace(state, preparation_deadline_stale=stale or state.preparation_deadline_stale,
                                                     alarm_reminder_id=alarm), self.now)

    def event_cancelled(self, event: dict[str, Any]) -> None:
        for state in self.groups.event_states_for_event(event["id"]):
            if state.remind_before_minutes is not None:
                self.set_reminder(state.account_id, reminder_key(SharedKind.SHARED_EVENT, event["id"]), None)
            if state.alarm_reminder_id:
                self.set_alarm(state, event["title"], None)

    def obligation_moved(self, item: dict[str, Any], old: datetime, new: datetime) -> None:
        for state in self.groups.obligation_states_for(item["id"]):
            moved = self.move_task(state.account_id, state.personal_task_id, old, new)
            if state.remind_before_minutes is not None:
                self.set_reminder(state.account_id, reminder_key(SharedKind.SHARED_OBLIGATION, item["id"]),
                                  lead_moment(new, state.remind_before_minutes, self.now))
            if moved is False and not state.personal_deadline_stale:
                self.groups.save_obligation_state(replace(state, personal_deadline_stale=True), self.now)

    def obligation_cancelled(self, item: dict[str, Any]) -> None:
        for state in self.groups.obligation_states_for(item["id"]):
            if state.remind_before_minutes is not None:
                self.set_reminder(state.account_id, reminder_key(SharedKind.SHARED_OBLIGATION, item["id"]), None)

    # ---- notifications ------------------------------------------------------------------

    def notify(self, group: Group, *, kind: SharedKind, entity: dict[str, Any], dedupe: str,
               compose: Callable[[str, str, bool], dict[str, str] | None], critical: bool, exclude: str | None,
               recipients: list[str] | None = None, respect_behavior: bool = True) -> int:
        """One message per subscribed member; muted groups/items and hidden categories are skipped."""
        from student_execution_os.reminders.store import ReminderStore
        store = ReminderStore(self.repo)
        sent = 0
        for account_id in recipients if recipients is not None else self.groups.active_member_ids(group.id):
            if account_id == exclude:
                continue
            if respect_behavior and not self._subscribed(account_id, group, kind, entity):
                continue
            prefs = store.prefs(account_id)
            urgent = self._critical_for(account_id, kind, entity, critical)
            content = compose(prefs.locale, prefs.timezone_name, urgent)
            if content is None:
                continue
            message = {**content, "deep_link": f"/groups/{group.id}", "actions": [{"id": "OPEN", "label":
                       "Открыть" if prefs.locale == "ru" else "Open", "background": False}]}
            if store.add_message(account_id, stage="GROUP_CRITICAL" if urgent else "GROUP_UPDATE",
                                 task_ids=[f"group:{entity.get('id', group.id)}"], content=message,
                                 dedupe_key=f"group:{dedupe}", now=self.now):
                sent += 1
        return sent

    def _critical_for(self, account_id: str, kind: SharedKind, entity: dict[str, Any], default: bool) -> bool:
        """How loud the notice is follows the member's own criticality, not only the group's."""
        if kind is SharedKind.SHARED_EVENT and "event_kind" in entity:
            override = self.groups.event_state(account_id, entity["id"]).criticality_override
        elif kind is SharedKind.SHARED_OBLIGATION and "deadline" in entity:
            override = self.groups.obligation_state(account_id, entity["id"]).criticality_override
        else:
            return default
        return default if override is None else override is Criticality.CRITICAL

    def _subscribed(self, account_id: str, group: Group, kind: SharedKind, entity: dict[str, Any]) -> bool:
        prefs = self.groups.preferences(account_id, group)
        if prefs.muted or prefs.notification_behavior is NotificationBehavior.SILENT:
            return False
        if kind is SharedKind.SHARED_EVENT:
            if not prefs.shows_event(EventKind(entity["event_kind"])):
                return False
            return not self.groups.event_state(account_id, entity["id"]).muted
        if kind is SharedKind.SHARED_OBLIGATION:
            return prefs.show_deadlines and not self.groups.obligation_state(account_id, entity["id"]).muted
        return prefs.show_announcements

    def retract_messages(self, entity_id: str) -> None:
        """Stop undelivered notifications about an announcement that was retracted."""
        self.repo.connection.execute(
            "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
            "last_error='RETRACTED' WHERE delivery_state='PENDING' AND EXISTS (SELECT 1 FROM json_each(task_ids_json) "
            "WHERE value=?)", (f"group:{entity_id}",),
        )


def event_critical(entity: dict[str, Any]) -> bool:
    return entity.get("group_criticality") == Criticality.CRITICAL.value


def notable_event(entity: dict[str, Any]) -> bool:
    """A newly published event is pushed when it matters (assessment, important/critical); a
    regular class only appears in the agenda."""
    return EventKind(entity["event_kind"]) in ASSESSMENT_KINDS or entity["group_criticality"] != Criticality.NORMAL.value


def instant(value: str) -> datetime:
    return _dt(value)

