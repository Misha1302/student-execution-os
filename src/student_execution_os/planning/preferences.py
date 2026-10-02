"""Soft planning preferences: canonical user wishes about *where* work goes.

A preference never changes what is possible. Feasibility (FEASIBLE / INFEASIBLE /
UNKNOWN) is always decided by the hard model alone; preferences only choose
between legal placements when the hard model has already found a witness. When
no placement honours a preference, that preference is relaxed and reported —
it can never turn a feasible plan into INFEASIBLE or UNKNOWN.

Each preference is a stored, versioned row (``planning_preferences``, schema
v30). The planner sees its derived, UTC-expanded windows
(:class:`PreferenceWindow`) through the planning snapshot, so a changed
preference changes the plan input hash like any other canonical input.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import HalfOpenInterval, Importance, ObligationCategory

MAX_ACTIVE_PREFERENCES = 20
MAX_SPAN_DAYS = 366
# "Сделай день полегче" without a number: a light day plans at most this much work.
LIGHT_DAY_WORK_MINUTES = 180
# A task is DEMANDING when it is important, an exam, or long: the planner keeps it
# out of windows the user wants for easier work (e.g. right after waking up).
DEMANDING_IMPORTANCE = frozenset({Importance.CRITICAL, Importance.HIGH})
DEMANDING_EFFORT_MINUTES = 120
STUDY_CATEGORIES = frozenset({ObligationCategory.HOMEWORK, ObligationCategory.LESSON, ObligationCategory.EXAM})
CLASS_CATEGORIES = frozenset({ObligationCategory.LESSON, ObligationCategory.EXAM})


class PreferenceKind(StrEnum):
    KEEP_FREE = "KEEP_FREE"                  # keep >= minutes contiguous free inside a daily window
    WORK_LIMIT = "WORK_LIMIT"                # plan at most `minutes` of work per local day
    AVOID_WORK = "AVOID_WORK"                # no `target` work inside a daily window
    REST_AFTER_EVENTS = "REST_AFTER_EVENTS"  # no work for `minutes` after each `target` event


class PreferenceAnchor(StrEnum):
    CLOCK = "CLOCK"  # window_start/window_end are local clock times
    WAKE = "WAKE"    # the window starts when the user's first planning window starts


_TARGETS = {
    PreferenceKind.KEEP_FREE: {"ALL"},
    PreferenceKind.WORK_LIMIT: {"ALL"},
    PreferenceKind.AVOID_WORK: {"ALL", "DEMANDING", "STUDY"},
    PreferenceKind.REST_AFTER_EVENTS: {"CLASSES", "ALL_EVENTS"},
}


def _hhmm(value: object, name: str) -> time:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be HH:MM")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValidationError(f"{name} must use minute precision")
    return parsed


def _day(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be YYYY-MM-DD") from exc


@dataclass(frozen=True)
class PlanningPreference:
    id: str
    account_id: str
    kind: PreferenceKind
    date_from: date
    date_until: date | None = None
    anchor: PreferenceAnchor = PreferenceAnchor.CLOCK
    target: str = "ALL"
    window_start: time | None = None
    window_end: time | None = None  # None = end of the local day
    minutes: int | None = None
    reason: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.id or not self.account_id:
            raise ValidationError("preference id and account_id are required")
        if self.version < 1:
            raise ValidationError("preference version must be >= 1")
        if self.target not in _TARGETS[self.kind]:
            raise ValidationError(f"{self.kind.value} does not apply to {self.target}")
        if self.date_until is not None:
            if self.date_until < self.date_from:
                raise ValidationError("preference date_until is before date_from")
            if (self.date_until - self.date_from).days >= MAX_SPAN_DAYS:
                raise ValidationError("preference spans more than a year; leave date_until empty for 'always'")
        if self.window_start is not None and self.window_end is not None and self.window_end <= self.window_start:
            raise ValidationError("preference window must end after it starts")
        minutes = self.minutes
        if self.kind is PreferenceKind.KEEP_FREE:
            if self.anchor is not PreferenceAnchor.CLOCK or self.window_start is None or self.window_end is None:
                raise ValidationError("KEEP_FREE needs a clock window")
            span = (datetime.combine(date.min, self.window_end) - datetime.combine(date.min, self.window_start))
            if minutes is None or not 15 <= minutes <= span.total_seconds() // 60:
                raise ValidationError("KEEP_FREE minutes must be 15 or more and fit inside the window")
        elif self.kind is PreferenceKind.WORK_LIMIT:
            if self.anchor is not PreferenceAnchor.CLOCK or self.window_start or self.window_end:
                raise ValidationError("WORK_LIMIT applies to whole local days")
            if minutes is None or not 0 <= minutes <= 960:
                raise ValidationError("WORK_LIMIT minutes must be between 0 and 960")
        elif self.kind is PreferenceKind.AVOID_WORK:
            if self.anchor is PreferenceAnchor.WAKE:
                if self.window_start or self.window_end:
                    raise ValidationError("a WAKE-anchored window is given by minutes, not clock times")
                if minutes is None or not 15 <= minutes <= 240:
                    raise ValidationError("minutes after waking must be between 15 and 240")
            else:
                if self.window_start is None or minutes is not None:
                    raise ValidationError("AVOID_WORK needs window_start (and optional window_end)")
        elif self.kind is PreferenceKind.REST_AFTER_EVENTS:
            if self.anchor is not PreferenceAnchor.CLOCK or self.window_start or self.window_end:
                raise ValidationError("REST_AFTER_EVENTS applies to whole local days")
            if minutes is None or not 5 <= minutes <= 120:
                raise ValidationError("rest after events must be between 5 and 120 minutes")

    def active_on(self, day: date) -> bool:
        return self.date_from <= day and (self.date_until is None or day <= self.date_until)

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "anchor": self.anchor.value,
            "target": self.target,
            "date_from": self.date_from.isoformat(),
            "date_until": self.date_until.isoformat() if self.date_until else None,
            "window_start": self.window_start.strftime("%H:%M") if self.window_start else None,
            "window_end": self.window_end.strftime("%H:%M") if self.window_end else None,
            "minutes": self.minutes,
            "reason": self.reason,
            "version": self.version,
            "ownership": "CANONICAL",
        }


def preference_from_payload(preference_id: str, account_id: str, payload: dict[str, object],
                            version: int = 1) -> PlanningPreference:
    """The one parser for preference fields (sync operations, Assistant actions, storage)."""
    allowed = {"kind", "anchor", "target", "date_from", "date_until", "window_start", "window_end", "minutes", "reason"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValidationError("preference fields are not supported: " + ", ".join(sorted(unknown)))
    try:
        kind = PreferenceKind(str(payload.get("kind") or ""))
        anchor = PreferenceAnchor(str(payload.get("anchor") or "CLOCK"))
    except ValueError:
        raise ValidationError("unknown preference kind or anchor") from None
    minutes = payload.get("minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
        raise ValidationError("preference minutes must be an integer")
    reason = payload.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ValidationError("preference reason must be text up to 500 characters")
    target = payload.get("target")
    if target is None:
        target = "CLASSES" if kind is PreferenceKind.REST_AFTER_EVENTS else "ALL"
    return PlanningPreference(
        id=preference_id,
        account_id=account_id,
        kind=kind,
        anchor=anchor,
        target=str(target),
        date_from=_day(payload.get("date_from"), "date_from"),
        date_until=_day(payload["date_until"], "date_until") if payload.get("date_until") else None,
        window_start=_hhmm(payload["window_start"], "window_start") if payload.get("window_start") else None,
        window_end=_hhmm(payload["window_end"], "window_end") if payload.get("window_end") else None,
        minutes=minutes,
        reason=reason or None,
        version=version,
    )


@dataclass(frozen=True, order=True)
class PreferenceWindow:
    """One preference expanded onto one local day, in UTC (derived, never stored)."""

    starts_at: datetime
    ends_at: datetime
    preference_id: str
    kind: PreferenceKind
    target: str = "ALL"
    minutes: int | None = None

    @property
    def interval(self) -> HalfOpenInterval:
        return HalfOpenInterval(self.starts_at, self.ends_at)


def _utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, at, zone).astimezone(timezone.utc)


def _floor_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def expand_preferences(preferences, *, timezone_name: str, planning_windows: dict[str, list[list[str]]],
                       start: datetime, end: datetime, events=()) -> tuple[PreferenceWindow, ...]:
    """Expand canonical preferences onto every local day overlapping [start, end)."""
    if end <= start or not preferences:
        return ()
    zone = ZoneInfo(timezone_name)
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date() + timedelta(days=1)
    windows: list[PreferenceWindow] = []
    for preference in sorted(preferences, key=lambda p: p.id):
        day = first
        while day <= last:
            if preference.active_on(day):
                windows.extend(_expand_day(preference, day, zone, planning_windows, events))
            day += timedelta(days=1)
    clipped = []
    for window in windows:
        left, right = max(window.starts_at, start), min(window.ends_at, end)
        left, right = _floor_minute(left), _floor_minute(right)
        if right > left:
            clipped.append(PreferenceWindow(left, right, window.preference_id, window.kind, window.target, window.minutes))
    return tuple(sorted(clipped))


def _expand_day(preference: PlanningPreference, day: date, zone: ZoneInfo, planning_windows, events):
    day_start = _utc(day, time.min, zone)
    day_end = _utc(day + timedelta(days=1), time.min, zone)
    kind = preference.kind
    if kind is PreferenceKind.WORK_LIMIT:
        return [PreferenceWindow(day_start, day_end, preference.id, kind, "ALL", preference.minutes)]
    if kind is PreferenceKind.REST_AFTER_EVENTS:
        result = []
        for event in events:
            category = event.obligation.category
            if preference.target == "CLASSES" and category not in CLASS_CATEGORIES:
                continue
            finish = event.interval.ends_at
            if day_start <= finish < day_end:
                result.append(PreferenceWindow(finish, finish + timedelta(minutes=preference.minutes or 0),
                                               preference.id, kind, "ALL"))
        return result
    if preference.anchor is PreferenceAnchor.WAKE:
        today = planning_windows.get(str(day.isoweekday())) or []
        if not today:
            return []
        wake = _utc(day, time.fromisoformat(today[0][0]), zone)
        return [PreferenceWindow(wake, wake + timedelta(minutes=preference.minutes or 0),
                                 preference.id, kind, preference.target)]
    left = _utc(day, preference.window_start or time.min, zone)
    right = _utc(day, preference.window_end, zone) if preference.window_end else day_end
    return [PreferenceWindow(left, right, preference.id, kind, preference.target, preference.minutes)]


def task_matches(target: str, task) -> bool:
    if target == "ALL":
        return True
    obligation = task.obligation
    if target == "STUDY":
        return obligation.category in STUDY_CATEGORIES
    if target == "DEMANDING":
        effort = task.estimated_total_effort_minutes or 0
        return (
            obligation.importance in DEMANDING_IMPORTANCE
            or obligation.category is ObligationCategory.EXAM
            or effort >= DEMANDING_EFFORT_MINUTES
        )
    return False


def _overlap_minutes(left: HalfOpenInterval, right: HalfOpenInterval) -> int:
    start, end = max(left.starts_at, right.starts_at), min(left.ends_at, right.ends_at)
    return max(0, int((end - start).total_seconds() // 60))


def _largest_gap(window: HalfOpenInterval, busy) -> int:
    cursor, best = window.starts_at, 0
    for item in sorted(busy, key=lambda i: (i.starts_at, i.ends_at)):
        if item.ends_at <= cursor or item.starts_at >= window.ends_at:
            continue
        if item.starts_at > cursor:
            best = max(best, int((item.starts_at - cursor).total_seconds() // 60))
        cursor = max(cursor, item.ends_at)
    if cursor < window.ends_at:
        best = max(best, int((window.ends_at - cursor).total_seconds() // 60))
    return best


@dataclass(frozen=True)
class PreferenceGuide:
    """Placement filter for one set of preference windows.

    ``admissible`` is consulted only to *choose* among placements the hard model
    already allows; it never adds capacity and is never used to decide feasibility.
    """

    windows: tuple[PreferenceWindow, ...]

    def unsatisfiable(self, hard_occupancy, pinned) -> tuple[str, ...]:
        """Preferences that hard facts already break (no work placement could honour them)."""
        broken: set[str] = set()
        pinned_intervals = [HalfOpenInterval(p.starts_at, p.ends_at) for p in pinned]
        for window in self.windows:
            if window.kind is PreferenceKind.KEEP_FREE:
                if _largest_gap(window.interval, hard_occupancy) < (window.minutes or 0):
                    broken.add(window.preference_id)
            elif window.kind is PreferenceKind.WORK_LIMIT:
                if sum(_overlap_minutes(window.interval, p) for p in pinned_intervals) > (window.minutes or 0):
                    broken.add(window.preference_id)
        return tuple(sorted(broken))

    def admissible(self, task, candidate, occupancy, placements) -> bool:
        interval = HalfOpenInterval(candidate.starts_at, candidate.ends_at)
        for window in self.windows:
            if not interval.overlaps(window.interval):
                continue
            if window.kind in (PreferenceKind.AVOID_WORK, PreferenceKind.REST_AFTER_EVENTS):
                if task_matches(window.target, task):
                    return False
            elif window.kind is PreferenceKind.WORK_LIMIT:
                planned = sum(
                    _overlap_minutes(window.interval, HalfOpenInterval(p.starts_at, p.ends_at)) for p in placements
                )
                if planned + _overlap_minutes(window.interval, interval) > (window.minutes or 0):
                    return False
            elif window.kind is PreferenceKind.KEEP_FREE:
                if _largest_gap(window.interval, [*occupancy, interval]) < (window.minutes or 0):
                    return False
        return True

    def without(self, preference_ids) -> "PreferenceGuide":
        dropped = set(preference_ids)
        return PreferenceGuide(tuple(w for w in self.windows if w.preference_id not in dropped))

    @property
    def preference_ids(self) -> tuple[str, ...]:
        return tuple(sorted({w.preference_id for w in self.windows}))


_RELAX_ORDER = {
    # Relaxed first → last: the least specific wish gives way first.
    PreferenceKind.WORK_LIMIT: 0,
    PreferenceKind.KEEP_FREE: 1,
    PreferenceKind.AVOID_WORK: 2,
    PreferenceKind.REST_AFTER_EVENTS: 3,
}


def relaxation_order(windows) -> tuple[str, ...]:
    kinds: dict[str, int] = {}
    for window in windows:
        rank = _RELAX_ORDER[window.kind]
        kinds[window.preference_id] = min(rank, kinds.get(window.preference_id, rank))
    return tuple(sorted(kinds, key=lambda pid: (kinds[pid], pid)))
