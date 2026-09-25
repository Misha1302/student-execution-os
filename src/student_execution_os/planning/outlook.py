from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory, HalfOpenInterval, UserTimeConstraint, UserTimeConstraintType
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso


DEFAULT_WINDOWS = {str(day): [["08:00", "22:00"]] for day in range(1, 8)}
OPTIONAL_POLICIES = {"FAIL_CLOSED", "OMIT_OPTIONAL", "OMIT_OPTIONAL_AND_PREFERRED"}


def _validate_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("unknown IANA timezone") from exc


def _parse_hhmm(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("planning window times must be HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValidationError("planning windows use minute precision")
    return parsed


def validate_windows(raw: object) -> dict[str, list[list[str]]]:
    if not isinstance(raw, dict) or set(raw) != {str(day) for day in range(1, 8)}:
        raise ValidationError("planning windows must define ISO weekdays 1-7")
    result: dict[str, list[list[str]]] = {}
    for day, windows in raw.items():
        if not isinstance(windows, list):
            raise ValidationError("weekday windows must be a list")
        normalized: list[list[str]] = []
        previous_end: time | None = None
        for pair in windows:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
                raise ValidationError("each planning window must be [start,end]")
            start, end = map(_parse_hhmm, pair)
            if start >= end or (previous_end is not None and start < previous_end):
                raise ValidationError("planning windows must be ordered, non-overlapping and non-empty")
            previous_end = end
            normalized.append([start.strftime("%H:%M"), end.strftime("%H:%M")])
        result[day] = normalized
    return result


@dataclass(frozen=True)
class PlanningProfile:
    timezone_name: str
    first_day_of_week: int
    windows: dict[str, list[list[str]]]
    optional_event_policy: str
    policy_disclosure: str
    version: int

    def payload(self) -> dict[str, object]:
        return {
            "timezone": self.timezone_name,
            "first_day_of_week": self.first_day_of_week,
            "planning_windows": self.windows,
            "optional_event_policy": self.optional_event_policy,
            "policy_disclosure": self.policy_disclosure,
            "version": self.version,
        }


class SQLitePlanningProfileRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def get(self, account_id: str) -> PlanningProfile:
        self.canonical._require_account(account_id)
        row = self.canonical.connection.execute(
            "SELECT * FROM planning_profiles WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            now = self.canonical.clock.now()
            with self.canonical._tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO planning_profiles(account_id,timezone_name,windows_json,updated_at) VALUES (?,?,?,?)",
                    (account_id, "UTC", json.dumps(DEFAULT_WINDOWS, sort_keys=True), _iso(now)),
                )
            row = self.canonical.connection.execute(
                "SELECT * FROM planning_profiles WHERE account_id=?", (account_id,)
            ).fetchone()
        return PlanningProfile(
            timezone_name=row["timezone_name"],
            first_day_of_week=int(row["first_day_of_week"]),
            windows=validate_windows(json.loads(row["windows_json"])),
            optional_event_policy=row["optional_event_policy"],
            policy_disclosure=row["policy_disclosure"],
            version=int(row["version"]),
        )

    def update(self, account_id: str, payload: dict[str, object]) -> PlanningProfile:
        current = self.get(account_id)
        expected = int(payload["expected_version"])
        if current.version != expected:
            raise VersionConflict("planning profile version changed")
        zone = str(payload.get("timezone", current.timezone_name))
        _validate_zone(zone)
        first = int(payload.get("first_day_of_week", current.first_day_of_week))
        if first not in range(1, 8):
            raise ValidationError("first_day_of_week must be in 1-7")
        windows = validate_windows(payload.get("planning_windows", current.windows))
        policy = str(payload.get("optional_event_policy", current.optional_event_policy))
        if policy not in OPTIONAL_POLICIES:
            raise ValidationError("unknown optional event policy")
        now = self.canonical.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE planning_profiles SET timezone_name=?,first_day_of_week=?,windows_json=?,optional_event_policy=?,"
                "policy_disclosure=?,version=version+1,updated_at=? WHERE account_id=? AND version=?",
                (zone, first, json.dumps(windows, sort_keys=True), policy,
                 "Configured planning windows; capacity outside them is excluded", _iso(now), account_id, expected),
            )
            if cur.rowcount != 1:
                raise VersionConflict("planning profile version changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="PLANNING_PROFILE", entity_id=account_id,
                action="UPDATE_PLANNING_PROFILE", actor=ActorCategory.USER_UI,
            )
        return self.get(account_id)


def planning_intervals(profile: PlanningProfile, start: date, days: int) -> list[tuple[datetime, datetime]]:
    zone = _validate_zone(profile.timezone_name)
    intervals: list[tuple[datetime, datetime]] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for left, right in profile.windows[str(day.isoweekday())]:
            local_start = datetime.combine(day, _parse_hhmm(left), zone)
            local_end = datetime.combine(day, _parse_hhmm(right), zone)
            intervals.append((local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)))
    return intervals


def overlap_minutes(left: tuple[datetime, datetime], right: tuple[datetime, datetime]) -> int:
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    return max(0, int((end - start).total_seconds() // 60))


OFF_HOURS_PREFIX = "off-hours:"


def off_hours_constraints(profile: PlanningProfile, account_id: str, start: datetime,
                          end: datetime) -> tuple[UserTimeConstraint, ...]:
    """The time outside the user's planning windows (sleep, by default 22:00–08:00)
    between ``start`` and ``end`` as UNAVAILABLE constraints.

    They are derived from the profile on every planning run and never stored, so a
    changed sleep schedule applies to the next plan immediately.
    """
    if end <= start:
        return ()
    zone = _validate_zone(profile.timezone_name)
    first = start.astimezone(zone).date() - timedelta(days=1)
    days = (end.astimezone(zone).date() - first).days + 2
    windows = sorted(planning_intervals(profile, first, days))
    gaps: list[tuple[datetime, datetime]] = []
    cursor = datetime.combine(first, time.min, zone).astimezone(timezone.utc)
    for left, right in windows:
        if left > cursor:
            gaps.append((cursor, left))
        cursor = max(cursor, right)
    horizon_end = datetime.combine(first + timedelta(days=days), time.min, zone).astimezone(timezone.utc)
    if cursor < horizon_end:
        gaps.append((cursor, horizon_end))
    result = []
    for left, right in gaps:
        left, right = max(left, start), min(right, end)
        if right - left < timedelta(minutes=1):
            continue
        left = left.replace(second=0, microsecond=0)
        right = right.replace(second=0, microsecond=0)
        if right <= left:
            continue
        result.append(UserTimeConstraint(
            id=f"{OFF_HOURS_PREFIX}{left.isoformat()}", account_id=account_id,
            type=UserTimeConstraintType.UNAVAILABLE, interval=HalfOpenInterval(left, right),
            obligation_id=None, reason="OFF_HOURS", version=1,
        ))
    return tuple(result)
