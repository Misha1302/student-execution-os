"""The personal projection of group facts for one account.

    shared fact (group-owned)  +  the member's overlay (user-owned)  →  agenda item

It is built only for the account that asks and is the one source for the Agenda
(``/api/v1/me/shared``), the planner (derived busy time) and the reminder engine
(shared reminder facts). No member ever sees another member's overlay through it.

An event bound to an external calendar that the member has imported themselves is
presented as that local event plus the annotation (``external.local_event_id``): one
logical item, with the official time taken from the member's own import.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.domain.model import HalfOpenInterval, LifecycleStatus, UserTimeConstraint, UserTimeConstraintType
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt

from . import external
from .fanout import EVENT_KEY, OBLIGATION_KEY
from .model import (
    BLOCKING_ATTENDANCE,
    AcceptanceState,
    Attendance,
    Capability,
    Criticality,
    EventKind,
    Group,
    GroupStatus,
    MembershipStatus,
    NotificationBehavior,
    SharedKind,
    UserGroupPreferences,
    capabilities,
    effective_attendance,
    effective_criticality,
    is_assessment,
)
from .repository import SQLiteGroupRepository

# A cancelled/retracted item stays visible this long, so the member sees that it was cancelled.
CLOSED_VISIBLE_FOR = timedelta(days=14)
SHARED_CONSTRAINT_PREFIX = "shared:"
OPEN_TASK = (LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT)


def preferences_view(prefs: UserGroupPreferences) -> dict[str, Any]:
    return {
        "group_id": prefs.group_id, "muted": prefs.muted, "show_regular_classes": prefs.show_regular_classes,
        "show_assessments": prefs.show_assessments, "show_deadlines": prefs.show_deadlines,
        "show_announcements": prefs.show_announcements, "announcements_in_agenda": prefs.announcements_in_agenda,
        "default_attendance_behavior": prefs.default_attendance_behavior.value if prefs.default_attendance_behavior else None,
        "notification_behavior": prefs.notification_behavior.value, "version": prefs.version,
    }


def official_interval(repo: SQLiteCanonicalRepository, row: dict[str, Any]) -> tuple[datetime, datetime]:
    """Start/end of a shared event: from the external source when bound, else the group's own."""
    if row.get("external_binding_id"):
        binding = SQLiteGroupRepository(repo).binding(row["external_binding_id"])
        if binding is not None and binding["status"] == "ACTIVE":
            official = external.resolve_official(repo, binding)
            return official.starts_at, official.ends_at
    return _dt(row["starts_at"]), _dt(row["ends_at"])


class PersonalProjection:
    def __init__(self, repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> None:
        self.repo = repo
        self.me = account_id
        self.now = now
        self.groups = SQLiteGroupRepository(repo)
        self._copies: dict[tuple[str, str], str] | None = None

    # ---- groups -------------------------------------------------------------------------

    def memberships(self) -> list[dict[str, Any]]:
        from .views import group_view
        result = []
        for group, membership in self.groups.groups_of(self.me, [MembershipStatus.ACTIVE, MembershipStatus.PENDING]):
            caps = capabilities(membership.role, group.settings) if membership.status is MembershipStatus.ACTIVE else frozenset()
            view = group_view(group, membership, caps, member_count=len(self.groups.active_member_ids(group.id)))
            view["preferences"] = preferences_view(self.groups.preferences(self.me, group))
            if Capability.MODERATE_PROPOSALS in caps:
                view["pending_proposals"] = int(self.repo.connection.execute(
                    "SELECT count(*) FROM group_proposals WHERE group_id=? AND status='PENDING'", (group.id,)).fetchone()[0])
            result.append(view)
        return result

    def _active_groups(self) -> list[tuple[Group, frozenset[Capability]]]:
        return [(group, capabilities(membership.role, group.settings))
                for group, membership in self.groups.groups_of(self.me, [MembershipStatus.ACTIVE])
                if group.status in (GroupStatus.ACTIVE, GroupStatus.ARCHIVED)]

    # ---- items ----------------------------------------------------------------------------

    def items(self, *, group_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for group, caps in self._active_groups():
            if group_id is not None and group.id != group_id:
                continue
            prefs = self.groups.preferences(self.me, group)
            since = (self.now - CLOSED_VISIBLE_FOR).isoformat()
            events = [dict(r) for r in self.repo.connection.execute(
                "SELECT * FROM shared_events WHERE group_id=? AND (status='PUBLISHED' OR updated_at>=?) ORDER BY starts_at,id",
                (group.id, since)).fetchall()]
            obligations = [dict(r) for r in self.repo.connection.execute(
                "SELECT * FROM shared_obligations WHERE group_id=? AND (status='PUBLISHED' OR updated_at>=?) ORDER BY deadline,id",
                (group.id, since)).fetchall()]
            announcements = [dict(r) for r in self.repo.connection.execute(
                "SELECT * FROM shared_announcements WHERE group_id=? AND (status='PUBLISHED' OR updated_at>=?) "
                "ORDER BY created_at DESC,id", (group.id, since)).fetchall()]
            changes = self.groups.last_changes([r["id"] for r in events + obligations + announcements])
            event_states = self.groups.event_states(self.me, [r["id"] for r in events])
            obligation_states = self.groups.obligation_states(self.me, [r["id"] for r in obligations])
            announcement_states = self.groups.announcement_states(self.me, [r["id"] for r in announcements])
            out += [self._event(r, group, caps, prefs, event_states.get(r["id"]), changes.get(r["id"])) for r in events]
            out += [self._obligation(r, group, caps, prefs, obligation_states.get(r["id"]), changes.get(r["id"]))
                    for r in obligations]
            out += [self._announcement(r, group, caps, prefs, announcement_states.get(r["id"]), changes.get(r["id"]))
                    for r in announcements]
        return out

    def item(self, kind: SharedKind, entity_id: str) -> dict[str, Any] | None:
        row = self.groups.entity(kind, entity_id)
        if row is None:
            return None
        membership = self.groups.membership(row["group_id"], self.me)
        group = self.groups.find_group(row["group_id"])
        if group is None or membership is None or membership.status is not MembershipStatus.ACTIVE:
            raise EntityNotFound("shared item not found")
        caps = capabilities(membership.role, group.settings)
        prefs = self.groups.preferences(self.me, group)
        change = self.groups.last_changes([entity_id]).get(entity_id)
        if kind is SharedKind.SHARED_EVENT:
            return self._event(row, group, caps, prefs, self.groups.event_state(self.me, entity_id), change)
        if kind is SharedKind.SHARED_OBLIGATION:
            return self._obligation(row, group, caps, prefs, self.groups.obligation_state(self.me, entity_id), change)
        return self._announcement(row, group, caps, prefs,
                                  self.groups.announcement_states(self.me, [entity_id]).get(entity_id), change)

    def _task(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        try:
            task = self.repo.get_task(self.me, task_id)
        except EntityNotFound:
            return None
        return {"id": task_id, "title": task.obligation.title, "status": task.obligation.lifecycle_status.value,
                "open": task.obligation.lifecycle_status in OPEN_TASK,
                "deadline": task.actual_cutoff.at.isoformat() if task.actual_cutoff.at else None}

    def _copies_map(self) -> dict[tuple[str, str], str]:
        if self._copies is None:
            self._copies = external.local_copies(self.repo, self.me)
        return self._copies

    @staticmethod
    def _change(row: dict[str, Any], change: dict[str, Any] | None, last_seen: int) -> dict[str, Any] | None:
        if change is None or int(change["new_version"] or 0) <= last_seen:
            return None
        return change

    def _base(self, kind: str, row: dict[str, Any], group: Group, change: dict[str, Any] | None, last_seen: int) -> dict[str, Any]:
        return {"kind": kind, "id": row["id"], "group_id": group.id, "group_name": group.name, "source_label": group.name,
                "status": row["status"], "version": int(row["version"]), "title": row["title"],
                "author": self.groups.display_name(row["author_account_id"]),
                "change": self._change(row, change, last_seen), "updated_at": row["updated_at"],
                "group_archived": group.status is GroupStatus.ARCHIVED}

    def _event(self, row: dict[str, Any], group: Group, caps: frozenset[Capability], prefs: UserGroupPreferences,
               state, change) -> dict[str, Any]:
        from .model import UserSharedEventState
        state = state or UserSharedEventState(self.me, row["id"])
        kind = EventKind(row["event_kind"])
        binding = self.groups.binding(row["external_binding_id"]) if row["external_binding_id"] else None
        starts_at, ends_at, location = _dt(row["starts_at"]), _dt(row["ends_at"]), row["location"]
        external_view = None
        if binding is not None and binding["status"] == "ACTIVE":
            local_id = external.local_copy_for(self._copies_map(), binding)
            if local_id is not None:
                official = external.official_fields(self.repo, self.me, local_id)
            else:
                official = external.resolve_official(self.repo, binding)
            starts_at, ends_at, location = official.starts_at, official.ends_at, official.location
            external_view = {"bound": True, "local_event_id": local_id, "official_title": official.title,
                             "source_key": binding["external_source_key"], "occurrence_key": binding["external_occurrence_key"],
                             "owned_fields": ["starts_at", "ends_at", "timezone", "location", "title_official"]}
        attendance = effective_attendance(Attendance(row["attendance_default"]), prefs, state, kind)
        criticality = effective_criticality(Criticality(row["group_criticality"]), state.criticality_override)
        published = row["status"] == "PUBLISHED"
        writable = group.status is GroupStatus.ACTIVE
        preparation = self._task(state.preparation_task_id)
        actions = ["set_attendance", "set_criticality", "remind", "alarm", "hide" if not state.muted else "unhide"]
        if published and is_assessment(kind) and not (preparation and preparation["open"]):
            actions.append("prepare")
        if writable and Capability.PUBLISH_SHARED in caps:
            actions += ["edit_for_group", "cancel_for_group"] if published else []
            if published and binding is None:
                actions.append("reschedule_for_group")
        if writable and Capability.BIND_EXTERNAL in caps and published and binding is not None:
            actions.append("detach_external")
        return {
            **self._base("SHARED_EVENT", row, group, change, state.last_seen_version),
            "description": row["description"], "event_kind": kind.value, "is_assessment": is_assessment(kind),
            "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "timezone": row["timezone"],
            "location": location, "external": external_view,
            "attendance": {"group": row["attendance_default"], "effective": attendance.value,
                           "overridden": state.attendance_override is not None},
            "criticality": {"group": row["group_criticality"], "effective": criticality.value,
                            "overridden": state.criticality_override is not None},
            "personal": {"remind_before_minutes": state.remind_before_minutes,
                         "alarm_before_minutes": state.alarm_before_minutes, "hidden": state.muted,
                         "preparation_task": preparation, "preparation_deadline_stale": state.preparation_deadline_stale,
                         "suggest_remove_preparation": not published and bool(preparation and preparation["open"]),
                         "last_seen_version": state.last_seen_version, "version": state.version},
            "visible_in_agenda": not state.muted and prefs.shows_event(kind),
            "available_actions": actions,
        }

    def _obligation(self, row: dict[str, Any], group: Group, caps: frozenset[Capability], prefs: UserGroupPreferences,
                    state, change) -> dict[str, Any]:
        from .model import UserSharedObligationState
        state = state or UserSharedObligationState(self.me, row["id"])
        criticality = effective_criticality(Criticality(row["group_criticality"]), state.criticality_override)
        published = row["status"] == "PUBLISHED"
        task = self._task(state.personal_task_id)
        actions = ["set_criticality", "remind", "hide" if not state.muted else "unhide"]
        if published:
            actions += ["accept", "decline"]
            if not (task and task["open"]):
                actions.append("take_on")
        if group.status is GroupStatus.ACTIVE and Capability.PUBLISH_SHARED in caps and published:
            actions += ["edit_for_group", "cancel_for_group"]
        return {
            **self._base("SHARED_OBLIGATION", row, group, change, state.last_seen_version),
            "description": row["description"], "obligation_kind": row["kind"], "deadline": row["deadline"],
            "timezone": row["timezone"], "estimated_effort_hint_minutes": row["estimated_effort_hint_minutes"],
            "criticality": {"group": row["group_criticality"], "effective": criticality.value,
                            "overridden": state.criticality_override is not None},
            "personal": {"acceptance_state": state.acceptance_state.value, "personal_task": task,
                         "deadline_stale": state.personal_deadline_stale, "remind_before_minutes": state.remind_before_minutes,
                         "hidden": state.muted, "suggest_remove_task": not published and bool(task and task["open"]),
                         "last_seen_version": state.last_seen_version, "version": state.version},
            "visible_in_agenda": prefs.show_deadlines and not state.muted
            and state.acceptance_state is not AcceptanceState.DECLINED,
            "available_actions": actions,
        }

    def _announcement(self, row: dict[str, Any], group: Group, caps: frozenset[Capability], prefs: UserGroupPreferences,
                      state, change) -> dict[str, Any]:
        state = state or {"dismissed": False, "last_seen_version": 0, "version": 0}
        published = row["status"] == "PUBLISHED"
        actions = ["dismiss" if not state["dismissed"] else "undismiss"]
        if group.status is GroupStatus.ACTIVE and Capability.PUBLISH_SHARED in caps and published:
            actions += ["edit_for_group", "retract_for_group"]
        return {
            **self._base("ANNOUNCEMENT", row, group, change, state["last_seen_version"]),
            # Informational: no start, no deadline — its own moment is only when it was published.
            "body": row["body"], "importance": row["importance"], "published_at": row["created_at"],
            "personal": {"dismissed": state["dismissed"], "last_seen_version": state["last_seen_version"],
                         "version": state["version"]},
            "visible_in_updates": prefs.show_announcements,
            "visible_in_agenda": published and prefs.show_announcements and prefs.announcements_in_agenda
            and not state["dismissed"],
            "available_actions": actions,
        }

    # ---- planner ----------------------------------------------------------------------------

    def busy_constraints(self, start: datetime, end: datetime) -> tuple[UserTimeConstraint, ...]:
        """Shared events the member attends (effective REQUIRED/PREFERRED) as derived busy time.

        Never stored; an event the member imported themselves is already a local Event
        and is not counted twice.
        """
        result = []
        for item in self.items():
            if item["kind"] != "SHARED_EVENT" or item["status"] != "PUBLISHED" or not item["visible_in_agenda"]:
                continue
            if (item["external"] or {}).get("local_event_id"):
                continue
            if Attendance(item["attendance"]["effective"]) not in BLOCKING_ATTENDANCE:
                continue
            s, e = datetime.fromisoformat(item["starts_at"]), datetime.fromisoformat(item["ends_at"])
            if e <= start or s >= end:
                continue
            result.append(UserTimeConstraint(
                id=f"{SHARED_CONSTRAINT_PREFIX}{item['id']}", account_id=self.me, type=UserTimeConstraintType.UNAVAILABLE,
                interval=HalfOpenInterval(s, e), obligation_id=None, reason=f"{item['group_name']}: {item['title']}",
                version=1))
        return tuple(result)

    # ---- reminders ----------------------------------------------------------------------------

    def reminder_facts(self) -> list:
        """Shared items the reminder engine should look at for this member.

        A shared item reaches the engine when the member asked for a reminder, or when it is
        CRITICAL for them and they opted into escalation for the group. An item whose own
        personal task (preparation / taken-on deadline) is open is escalated through that task
        instead, so no prompt is sent twice.
        """
        from student_execution_os.reminders.policy import TaskFacts
        facts = []
        for item in self.items():
            if item["kind"] == "ANNOUNCEMENT" or item["status"] != "PUBLISHED" or not item["visible_in_agenda"]:
                continue
            group = self.groups.find_group(item["group_id"])
            prefs = self.groups.preferences(self.me, group)
            personal = item["personal"]
            requested = personal.get("remind_before_minutes") is not None
            task = personal.get("preparation_task") or personal.get("personal_task")
            ladder = (item["criticality"]["effective"] == Criticality.CRITICAL.value and not prefs.muted
                      and prefs.notification_behavior is NotificationBehavior.CHANGES_AND_CRITICAL
                      and not (task and task["open"]))
            if item["kind"] == "SHARED_EVENT" and Attendance(item["attendance"]["effective"]) is Attendance.SKIP:
                ladder = False
            if not requested and not ladder:
                continue
            is_event = item["kind"] == "SHARED_EVENT"
            facts.append(TaskFacts(
                task_id=(EVENT_KEY if is_event else OBLIGATION_KEY) + item["id"], title=item["title"], status="ACTIVE",
                cutoff_at=None, target_at=datetime.fromisoformat(item["starts_at"] if is_event else item["deadline"]),
                latest_safe_start=None, risk_state=None, remaining_minutes=None, started_at=None, last_progress_at=None,
                actionable_from=None, kind="SHARED_EVENT" if is_event else "SHARED_OBLIGATION",
                ends_at=datetime.fromisoformat(item["ends_at"]) if is_event else None,
                importance=Criticality.CRITICAL.value if ladder else "NORMAL",
            ))
        return facts


def shared_busy(repo: SQLiteCanonicalRepository, account_id: str, now: datetime):
    """A ``derived_constraints`` callable for the planning snapshot."""
    projection = PersonalProjection(repo, account_id, now)
    return lambda start, end: projection.busy_constraints(start, end)
