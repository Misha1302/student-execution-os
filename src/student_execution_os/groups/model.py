"""Collaborative groups: the group layer and the personal overlay (schema v18, ADR 0021).

    GROUP tells the user WHAT is happening.
    PERSONAL OS decides WHAT THE USER should do about it.

The group layer owns facts: ``SharedEvent`` (something at a time), ``SharedObligation``
(a result due by a moment) and ``SharedAnnouncement`` (information). A member's
suggestion is a separate ``GroupProposal``; nothing is published until it is approved.
Every member keeps a private overlay (``UserSharedEventState`` …) that the group can
neither read nor change.

This module is pure: typed values, capabilities, the normative state machines and the
validation shared by direct publication and proposals. Persistence and authorization
live in ``repository.py`` / ``commands.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import ValidationError


class GroupType(StrEnum):
    ACADEMIC = "ACADEMIC"
    PROJECT = "PROJECT"
    OTHER = "OTHER"


class GroupStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"  # terminal tombstone: the row stays for dedup/audit


class Role(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    SCHEDULER = "SCHEDULER"
    MEMBER = "MEMBER"


class MembershipStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    LEFT = "LEFT"
    REMOVED = "REMOVED"
    BLOCKED = "BLOCKED"


class SharedStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    RETRACTED = "RETRACTED"  # announcements only


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class SharedKind(StrEnum):
    """Kind of a group-owned fact (also the proposal kind and the approved entity kind)."""
    SHARED_EVENT = "SHARED_EVENT"
    SHARED_OBLIGATION = "SHARED_OBLIGATION"
    SHARED_ANNOUNCEMENT = "SHARED_ANNOUNCEMENT"


_MAX_EVENT_SPAN = timedelta(days=7)


class EventKind(StrEnum):
    CLASS = "CLASS"
    LECTURE = "LECTURE"
    SEMINAR = "SEMINAR"
    PRACTICE = "PRACTICE"
    LAB = "LAB"
    CONSULTATION = "CONSULTATION"
    QUIZ = "QUIZ"
    TEST = "TEST"
    CONTROL_WORK = "CONTROL_WORK"
    COLLOQUIUM = "COLLOQUIUM"
    EXAM = "EXAM"
    GROUP_MEETING = "GROUP_MEETING"
    OTHER = "OTHER"


# An assessment is a SharedEvent with one of these kinds — not a separate entity.
ASSESSMENT_KINDS = frozenset({EventKind.QUIZ, EventKind.TEST, EventKind.CONTROL_WORK, EventKind.COLLOQUIUM, EventKind.EXAM})
# Regular classes (the "занятия" subscription switch).
CLASS_KINDS = frozenset({EventKind.CLASS, EventKind.LECTURE, EventKind.SEMINAR, EventKind.PRACTICE, EventKind.LAB,
                         EventKind.CONSULTATION})


def is_assessment(kind: EventKind | str) -> bool:
    return EventKind(kind) in ASSESSMENT_KINDS


class ObligationKind(StrEnum):
    ASSIGNMENT = "ASSIGNMENT"
    LAB_REPORT = "LAB_REPORT"
    HOMEWORK = "HOMEWORK"
    REGISTRATION = "REGISTRATION"
    SUBMISSION = "SUBMISSION"
    OTHER = "OTHER"


class EventSource(StrEnum):
    GROUP_MANUAL = "GROUP_MANUAL"
    EXTERNAL_ANNOTATION = "EXTERNAL_ANNOTATION"


class Attendance(StrEnum):
    """Attendance intent: independent of criticality."""
    REQUIRED = "REQUIRED"    # time is taken; never suggested to skip in normal conditions
    PREFERRED = "PREFERRED"  # blocked by default, may be offered as skippable in a serious conflict
    OPTIONAL = "OPTIONAL"    # weak preference, does not block planning
    SKIP = "SKIP"            # stays visible, never blocks planning


# Attendance intents that make the planner treat the time as taken.
BLOCKING_ATTENDANCE = frozenset({Attendance.REQUIRED, Attendance.PREFERRED})


class Criticality(StrEnum):
    """Consequences of missing/forgetting — independent of attendance."""
    NORMAL = "NORMAL"
    IMPORTANT = "IMPORTANT"
    CRITICAL = "CRITICAL"


class AnnouncementImportance(StrEnum):
    NORMAL = "NORMAL"
    IMPORTANT = "IMPORTANT"
    URGENT = "URGENT"


class AcceptanceState(StrEnum):
    UNDECIDED = "UNDECIDED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


class NotificationBehavior(StrEnum):
    SILENT = "SILENT"                              # nothing is pushed; items still show in the agenda
    CHANGES = "CHANGES"                            # changes/cancellations/new facts are notified (default)
    CHANGES_AND_CRITICAL = "CHANGES_AND_CRITICAL"  # plus the critical escalation ladder (opt-in)


class InviteKind(StrEnum):
    LINK = "LINK"
    CODE = "CODE"


class BindingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DETACHED = "DETACHED"


class RelationKind(StrEnum):
    ANNOTATES = "ANNOTATES"


# ---- capabilities ------------------------------------------------------------------


class Capability(StrEnum):
    VIEW_SHARED = "VIEW_SHARED"
    CREATE_PROPOSAL = "CREATE_PROPOSAL"
    PUBLISH_SHARED = "PUBLISH_SHARED"          # create/edit/cancel events, obligations, announcements
    MODERATE_PROPOSALS = "MODERATE_PROPOSALS"
    BIND_EXTERNAL = "BIND_EXTERNAL"
    VIEW_MEMBERS = "VIEW_MEMBERS"
    MANAGE_MEMBERS = "MANAGE_MEMBERS"          # approve pending, remove, block/unblock
    MANAGE_INVITES = "MANAGE_INVITES"
    MANAGE_SETTINGS = "MANAGE_SETTINGS"
    MANAGE_ROLES = "MANAGE_ROLES"
    ARCHIVE_GROUP = "ARCHIVE_GROUP"
    DELETE_GROUP = "DELETE_GROUP"
    VIEW_AUDIT = "VIEW_AUDIT"


_MEMBER = frozenset({Capability.VIEW_SHARED, Capability.CREATE_PROPOSAL, Capability.VIEW_MEMBERS})
_SCHEDULER = _MEMBER | {Capability.PUBLISH_SHARED, Capability.MODERATE_PROPOSALS, Capability.BIND_EXTERNAL,
                        Capability.VIEW_AUDIT}
_ADMIN = _SCHEDULER | {Capability.MANAGE_MEMBERS, Capability.MANAGE_INVITES, Capability.MANAGE_SETTINGS,
                       Capability.ARCHIVE_GROUP}

# Default capabilities per role (the role table of the specification). Authorization
# always asks for one capability; roles are never compared as an ordered scale.
ROLE_CAPABILITIES: Mapping[Role, frozenset[Capability]] = {
    Role.MEMBER: _MEMBER,
    Role.SCHEDULER: _SCHEDULER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: frozenset(Capability),
}


def capabilities(role: Role, settings: "GroupSettings") -> frozenset[Capability]:
    caps = ROLE_CAPABILITIES[role]
    if role is Role.MEMBER and settings.allow_members_to_publish:
        # A small trusted group lets everyone publish directly.
        caps = caps | {Capability.PUBLISH_SHARED}
    return caps


# ---- typed settings -----------------------------------------------------------------


def validate_timezone(value: Any, field_name: str = "timezone") -> str:
    name = str(value or "").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an IANA time zone") from exc
    return name


def _flag(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{field_name} must be true or false")
    return value


@dataclass(frozen=True)
class SubscriptionDefaults:
    show_regular_classes: bool = True
    show_assessments: bool = True
    show_deadlines: bool = True
    show_announcements: bool = True


@dataclass(frozen=True)
class GroupSettings:
    allow_members_to_publish: bool = False
    default_timezone: str = "UTC"
    default_subscription_preferences: SubscriptionDefaults = field(default_factory=SubscriptionDefaults)

    def __post_init__(self) -> None:
        validate_timezone(self.default_timezone, "default_timezone")

    def patched(self, payload: Mapping[str, Any]) -> "GroupSettings":
        unknown = set(payload) - {"allow_members_to_publish", "default_timezone", "default_subscription_preferences"}
        if unknown:
            raise ValidationError("unknown group settings: " + ", ".join(sorted(unknown)))
        result = self
        if "allow_members_to_publish" in payload:
            result = replace(result, allow_members_to_publish=_flag(payload["allow_members_to_publish"], "allow_members_to_publish"))
        if "default_timezone" in payload:
            result = replace(result, default_timezone=validate_timezone(payload["default_timezone"], "default_timezone"))
        if "default_subscription_preferences" in payload:
            raw = payload["default_subscription_preferences"]
            if not isinstance(raw, Mapping):
                raise ValidationError("default_subscription_preferences must be an object")
            names = {f for f in SubscriptionDefaults.__dataclass_fields__}
            unknown = set(raw) - names
            if unknown:
                raise ValidationError("unknown subscription defaults: " + ", ".join(sorted(unknown)))
            values = {k: _flag(v, k) for k, v in raw.items()}
            result = replace(result, default_subscription_preferences=replace(result.default_subscription_preferences, **values))
        return result


@dataclass(frozen=True)
class GroupJoinPolicy:
    invite_links_enabled: bool = True
    join_codes_enabled: bool = True
    approval_required: bool = False
    # LEFT members may always come back with a valid invite; REMOVED ones only when
    # this is on; BLOCKED ones only by an explicit admin action.
    allow_rejoin_after_removal: bool = False

    def patched(self, payload: Mapping[str, Any]) -> "GroupJoinPolicy":
        names = set(self.__dataclass_fields__)
        unknown = set(payload) - names
        if unknown:
            raise ValidationError("unknown join policy fields: " + ", ".join(sorted(unknown)))
        return replace(self, **{k: _flag(v, k) for k, v in payload.items()})


# ---- entities -----------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    description: str | None
    type: GroupType
    owner_account_id: str
    version: int
    status: GroupStatus
    settings: GroupSettings
    join_policy: GroupJoinPolicy
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Membership:
    group_id: str
    account_id: str
    role: Role
    status: MembershipStatus
    version: int
    joined_at: datetime
    updated_at: datetime


# ---- state machines (normative; mirrored in the tests) ------------------------------

GROUP_TRANSITIONS: Mapping[GroupStatus, frozenset[GroupStatus]] = {
    GroupStatus.ACTIVE: frozenset({GroupStatus.ARCHIVED, GroupStatus.DELETED}),
    GroupStatus.ARCHIVED: frozenset({GroupStatus.ACTIVE, GroupStatus.DELETED}),
    GroupStatus.DELETED: frozenset(),
}

MEMBERSHIP_TRANSITIONS: Mapping[MembershipStatus, frozenset[MembershipStatus]] = {
    # (+ LEFT: the applicant withdraws the join request themselves)
    MembershipStatus.PENDING: frozenset({MembershipStatus.ACTIVE, MembershipStatus.REMOVED, MembershipStatus.BLOCKED,
                                         MembershipStatus.LEFT}),
    MembershipStatus.ACTIVE: frozenset({MembershipStatus.LEFT, MembershipStatus.REMOVED, MembershipStatus.BLOCKED}),
    # Coming back: through an invite when the join policy allows it (see can_rejoin).
    MembershipStatus.LEFT: frozenset({MembershipStatus.ACTIVE, MembershipStatus.PENDING, MembershipStatus.BLOCKED}),
    MembershipStatus.REMOVED: frozenset({MembershipStatus.ACTIVE, MembershipStatus.PENDING, MembershipStatus.BLOCKED}),
    # Only an explicit admin action unblocks.
    MembershipStatus.BLOCKED: frozenset({MembershipStatus.ACTIVE, MembershipStatus.REMOVED}),
}

SHARED_TRANSITIONS: Mapping[SharedKind, Mapping[SharedStatus, frozenset[SharedStatus]]] = {
    SharedKind.SHARED_EVENT: {SharedStatus.PUBLISHED: frozenset({SharedStatus.PUBLISHED, SharedStatus.CANCELLED}),
                              SharedStatus.CANCELLED: frozenset()},
    SharedKind.SHARED_OBLIGATION: {SharedStatus.PUBLISHED: frozenset({SharedStatus.PUBLISHED, SharedStatus.CANCELLED}),
                                   SharedStatus.CANCELLED: frozenset()},
    SharedKind.SHARED_ANNOUNCEMENT: {SharedStatus.PUBLISHED: frozenset({SharedStatus.PUBLISHED, SharedStatus.RETRACTED}),
                                     SharedStatus.RETRACTED: frozenset()},
}

PROPOSAL_TRANSITIONS: Mapping[ProposalStatus, frozenset[ProposalStatus]] = {
    ProposalStatus.PENDING: frozenset({ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.WITHDRAWN}),
    ProposalStatus.APPROVED: frozenset(),
    ProposalStatus.REJECTED: frozenset(),
    ProposalStatus.WITHDRAWN: frozenset(),
}


class IllegalTransition(ValidationError):
    """The requested state change is not in the normative state machine."""

    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity} cannot go from {current} to {target}")
        self.entity = entity
        self.current = current
        self.target = target


def require_group_transition(current: GroupStatus, target: GroupStatus) -> None:
    if target not in GROUP_TRANSITIONS[current]:
        raise IllegalTransition("group", current.value, target.value)


def require_membership_transition(current: MembershipStatus, target: MembershipStatus) -> None:
    if target not in MEMBERSHIP_TRANSITIONS[current]:
        raise IllegalTransition("membership", current.value, target.value)


def require_shared_transition(kind: SharedKind, current: SharedStatus, target: SharedStatus) -> None:
    if target not in SHARED_TRANSITIONS[kind].get(current, frozenset()):
        raise IllegalTransition(kind.value.lower(), current.value, target.value)


def require_proposal_transition(current: ProposalStatus, target: ProposalStatus) -> None:
    if target not in PROPOSAL_TRANSITIONS[current]:
        raise IllegalTransition("proposal", current.value, target.value)


def can_rejoin(status: MembershipStatus, policy: GroupJoinPolicy) -> bool:
    """Whether an invite may bring back a member in ``status``."""
    if status is MembershipStatus.LEFT:
        return True
    if status is MembershipStatus.REMOVED:
        return policy.allow_rejoin_after_removal
    return False


# ---- typed payloads (direct publication and proposals share the validation) ---------


def parse_instant(value: Any, field_name: str) -> datetime:
    """An unambiguous timestamp; a naive local datetime is never interpreted."""
    if value is None or value == "":
        raise ValidationError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be an ISO-8601 instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field_name} must include a UTC offset")
    return parsed.replace(second=0, microsecond=0)


def clean_title(value: Any, field_name: str = "title", limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValidationError(f"{field_name} is required")
    if len(text) > limit:
        raise ValidationError(f"{field_name} is longer than {limit} characters")
    return text


def clean_text(value: Any, field_name: str, limit: int) -> str | None:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValidationError(f"{field_name} is longer than {limit} characters")
    return text or None


def _enum(cls, value: Any, field_name: str):
    try:
        return cls(value)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be one of: " + ", ".join(m.value for m in cls)) from exc


def default_criticality(kind: EventKind) -> Criticality:
    """Assessments are suggested as CRITICAL; a member may still override locally."""
    return Criticality.CRITICAL if kind in ASSESSMENT_KINDS else Criticality.NORMAL


def default_attendance(kind: EventKind) -> Attendance:
    if kind in ASSESSMENT_KINDS or kind in {EventKind.SEMINAR, EventKind.PRACTICE, EventKind.LAB}:
        return Attendance.REQUIRED
    return Attendance.PREFERRED


@dataclass(frozen=True)
class EventPayload:
    kind = SharedKind.SHARED_EVENT
    title: str
    event_kind: EventKind
    starts_at: datetime
    ends_at: datetime
    timezone: str
    attendance_default: Attendance
    group_criticality: Criticality
    description: str | None = None
    location: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "title": self.title, "event_kind": self.event_kind.value,
                "starts_at": self.starts_at.isoformat(), "ends_at": self.ends_at.isoformat(),
                "timezone": self.timezone, "attendance_default": self.attendance_default.value,
                "group_criticality": self.group_criticality.value, "description": self.description,
                "location": self.location}


@dataclass(frozen=True)
class ObligationPayload:
    kind = SharedKind.SHARED_OBLIGATION
    title: str
    obligation_kind: ObligationKind
    deadline: datetime
    timezone: str
    group_criticality: Criticality
    description: str | None = None
    estimated_effort_hint_minutes: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "title": self.title, "obligation_kind": self.obligation_kind.value,
                "deadline": self.deadline.isoformat(), "timezone": self.timezone,
                "group_criticality": self.group_criticality.value, "description": self.description,
                "estimated_effort_hint_minutes": self.estimated_effort_hint_minutes}


@dataclass(frozen=True)
class AnnouncementPayload:
    kind = SharedKind.SHARED_ANNOUNCEMENT
    title: str
    body: str
    importance: AnnouncementImportance

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "title": self.title, "body": self.body, "importance": self.importance.value}


SharedPayload = EventPayload | ObligationPayload | AnnouncementPayload

_EVENT_FIELDS = {"kind", "title", "event_kind", "starts_at", "ends_at", "timezone", "attendance_default",
                 "group_criticality", "description", "location"}
_OBLIGATION_FIELDS = {"kind", "title", "obligation_kind", "deadline", "timezone", "group_criticality", "description",
                      "estimated_effort_hint_minutes"}
_ANNOUNCEMENT_FIELDS = {"kind", "title", "body", "importance"}


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))


def parse_event_payload(raw: Mapping[str, Any], *, default_timezone: str) -> EventPayload:
    _reject_unknown(raw, _EVENT_FIELDS)
    event_kind = _enum(EventKind, raw.get("event_kind") or EventKind.OTHER.value, "event_kind")
    starts_at = parse_instant(raw.get("starts_at"), "starts_at")
    ends_at = parse_instant(raw.get("ends_at"), "ends_at")
    if ends_at <= starts_at:
        raise ValidationError("an event must end after it starts")
    if ends_at - starts_at > _MAX_EVENT_SPAN:
        raise ValidationError("a shared event cannot last longer than 7 days")
    return EventPayload(
        title=clean_title(raw.get("title")),
        event_kind=event_kind,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=validate_timezone(raw.get("timezone") or default_timezone),
        attendance_default=_enum(Attendance, raw.get("attendance_default") or default_attendance(event_kind).value,
                                 "attendance_default"),
        group_criticality=_enum(Criticality, raw.get("group_criticality") or default_criticality(event_kind).value,
                                "group_criticality"),
        description=clean_text(raw.get("description"), "description", 5000),
        location=clean_text(raw.get("location"), "location", 200),
    )


def parse_obligation_payload(raw: Mapping[str, Any], *, default_timezone: str) -> ObligationPayload:
    _reject_unknown(raw, _OBLIGATION_FIELDS)
    effort = raw.get("estimated_effort_hint_minutes")
    if effort in (None, ""):
        effort = None
    else:
        try:
            effort = int(effort)
        except (TypeError, ValueError) as exc:
            raise ValidationError("estimated_effort_hint_minutes must be whole minutes") from exc
        if not 1 <= effort <= 100_000:
            raise ValidationError("estimated_effort_hint_minutes is out of range")
    return ObligationPayload(
        title=clean_title(raw.get("title")),
        obligation_kind=_enum(ObligationKind, raw.get("obligation_kind") or ObligationKind.ASSIGNMENT.value, "obligation_kind"),
        deadline=parse_instant(raw.get("deadline"), "deadline"),
        timezone=validate_timezone(raw.get("timezone") or default_timezone),
        group_criticality=_enum(Criticality, raw.get("group_criticality") or Criticality.NORMAL.value, "group_criticality"),
        description=clean_text(raw.get("description"), "description", 5000),
        estimated_effort_hint_minutes=effort,
    )


def parse_announcement_payload(raw: Mapping[str, Any], *, default_timezone: str | None = None) -> AnnouncementPayload:
    _reject_unknown(raw, _ANNOUNCEMENT_FIELDS)
    body = clean_text(raw.get("body"), "body", 5000)
    if not body:
        raise ValidationError("body is required")
    return AnnouncementPayload(
        title=clean_title(raw.get("title")),
        body=body,
        importance=_enum(AnnouncementImportance, raw.get("importance") or AnnouncementImportance.NORMAL.value, "importance"),
    )


_PARSERS = {
    SharedKind.SHARED_EVENT: parse_event_payload,
    SharedKind.SHARED_OBLIGATION: parse_obligation_payload,
    SharedKind.SHARED_ANNOUNCEMENT: parse_announcement_payload,
}


def parse_payload(raw: Any, *, default_timezone: str, expected_kind: SharedKind | None = None) -> SharedPayload:
    """The tagged union of a proposal: ``kind`` selects the typed payload."""
    if not isinstance(raw, Mapping):
        raise ValidationError("payload must be an object")
    kind = _enum(SharedKind, raw.get("kind"), "payload.kind")
    if expected_kind is not None and kind is not expected_kind:
        raise ValidationError(f"payload.kind must be {expected_kind.value}")
    return _PARSERS[kind](raw, default_timezone=default_timezone)




# ---- personal overlay -----------------------------------------------------------------


@dataclass(frozen=True)
class UserGroupPreferences:
    account_id: str
    group_id: str
    muted: bool = False
    show_regular_classes: bool = True
    show_assessments: bool = True
    show_deadlines: bool = True
    show_announcements: bool = True
    announcements_in_agenda: bool = False
    default_attendance_behavior: Attendance | None = None
    notification_behavior: NotificationBehavior = NotificationBehavior.CHANGES
    version: int = 0

    def shows_event(self, kind: EventKind) -> bool:
        if kind in ASSESSMENT_KINDS:
            return self.show_assessments
        if kind in CLASS_KINDS:
            return self.show_regular_classes
        return True

    def patched(self, payload: Mapping[str, Any]) -> "UserGroupPreferences":
        flags = {"muted", "show_regular_classes", "show_assessments", "show_deadlines", "show_announcements",
                 "announcements_in_agenda"}
        allowed = flags | {"default_attendance_behavior", "notification_behavior", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("unknown preference fields: " + ", ".join(sorted(unknown)))
        values: dict[str, Any] = {k: _flag(payload[k], k) for k in flags if k in payload}
        if "default_attendance_behavior" in payload:
            raw = payload["default_attendance_behavior"]
            values["default_attendance_behavior"] = None if raw in (None, "") else _enum(Attendance, raw, "default_attendance_behavior")
        if "notification_behavior" in payload:
            values["notification_behavior"] = _enum(NotificationBehavior, payload["notification_behavior"], "notification_behavior")
        return replace(self, **values)


@dataclass(frozen=True)
class UserSharedEventState:
    account_id: str
    shared_event_id: str
    attendance_override: Attendance | None = None
    criticality_override: Criticality | None = None
    remind_before_minutes: int | None = None
    alarm_before_minutes: int | None = None
    alarm_reminder_id: str | None = None
    muted: bool = False
    preparation_task_id: str | None = None
    preparation_deadline_stale: bool = False
    last_seen_version: int = 0
    version: int = 0


@dataclass(frozen=True)
class UserSharedObligationState:
    account_id: str
    shared_obligation_id: str
    acceptance_state: AcceptanceState = AcceptanceState.UNDECIDED
    personal_task_id: str | None = None
    personal_deadline_stale: bool = False
    criticality_override: Criticality | None = None
    remind_before_minutes: int | None = None
    muted: bool = False
    last_seen_version: int = 0
    version: int = 0


MAX_LEAD_MINUTES = 10080


def parse_lead(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be whole minutes") from exc
    if not 0 <= minutes <= MAX_LEAD_MINUTES:
        raise ValidationError(f"{field_name} must be between 0 and {MAX_LEAD_MINUTES}")
    return minutes


EVENT_STATE_FIELDS = {"attendance_override", "criticality_override", "remind_before_minutes", "alarm_before_minutes",
                      "muted", "last_seen_version", "dismiss_stale_preparation"}
OBLIGATION_STATE_FIELDS = {"acceptance_state", "criticality_override", "remind_before_minutes", "muted",
                           "last_seen_version", "dismiss_stale_deadline"}


def patch_event_state(state: UserSharedEventState, payload: Mapping[str, Any]) -> UserSharedEventState:
    unknown = set(payload) - EVENT_STATE_FIELDS
    if unknown:
        raise ValidationError("fields cannot be set: " + ", ".join(sorted(unknown)))
    values: dict[str, Any] = {}
    if "attendance_override" in payload:
        raw = payload["attendance_override"]
        values["attendance_override"] = None if raw in (None, "") else _enum(Attendance, raw, "attendance_override")
    if "criticality_override" in payload:
        raw = payload["criticality_override"]
        values["criticality_override"] = None if raw in (None, "") else _enum(Criticality, raw, "criticality_override")
    if "remind_before_minutes" in payload:
        values["remind_before_minutes"] = parse_lead(payload["remind_before_minutes"], "remind_before_minutes")
    if "alarm_before_minutes" in payload:
        values["alarm_before_minutes"] = parse_lead(payload["alarm_before_minutes"], "alarm_before_minutes")
    if "muted" in payload:
        values["muted"] = _flag(payload["muted"], "muted")
    if "last_seen_version" in payload:
        values["last_seen_version"] = max(state.last_seen_version, _non_negative(payload["last_seen_version"], "last_seen_version"))
    if payload.get("dismiss_stale_preparation"):
        values["preparation_deadline_stale"] = False
    return replace(state, **values)


def patch_obligation_state(state: UserSharedObligationState, payload: Mapping[str, Any]) -> UserSharedObligationState:
    unknown = set(payload) - OBLIGATION_STATE_FIELDS
    if unknown:
        raise ValidationError("fields cannot be set: " + ", ".join(sorted(unknown)))
    values: dict[str, Any] = {}
    if "acceptance_state" in payload:
        values["acceptance_state"] = _enum(AcceptanceState, payload["acceptance_state"], "acceptance_state")
    if "criticality_override" in payload:
        raw = payload["criticality_override"]
        values["criticality_override"] = None if raw in (None, "") else _enum(Criticality, raw, "criticality_override")
    if "remind_before_minutes" in payload:
        values["remind_before_minutes"] = parse_lead(payload["remind_before_minutes"], "remind_before_minutes")
    if "muted" in payload:
        values["muted"] = _flag(payload["muted"], "muted")
    if "last_seen_version" in payload:
        values["last_seen_version"] = max(state.last_seen_version, _non_negative(payload["last_seen_version"], "last_seen_version"))
    if payload.get("dismiss_stale_deadline"):
        values["personal_deadline_stale"] = False
    return replace(state, **values)


def _non_negative(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be a whole number") from exc
    if number < 0:
        raise ValidationError(f"{field_name} cannot be negative")
    return number


def effective_attendance(default: Attendance, prefs: UserGroupPreferences | None, state: UserSharedEventState | None,
                         kind: EventKind) -> Attendance:
    """Personal override > personal default for regular classes > the group's default."""
    if state is not None and state.attendance_override is not None:
        return state.attendance_override
    if prefs is not None and prefs.default_attendance_behavior is not None and kind in CLASS_KINDS:
        return prefs.default_attendance_behavior
    return default


def effective_criticality(group_value: Criticality, override: Criticality | None) -> Criticality:
    return override or group_value


# Importance of the personal preparation/follow-up task created from a shared item.
TASK_IMPORTANCE = {Criticality.NORMAL: "NORMAL", Criticality.IMPORTANT: "HIGH", Criticality.CRITICAL: "CRITICAL"}
