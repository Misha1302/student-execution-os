"""RFC 5545 iCalendar -> provider-independent SourceSnapshot.

The parser deliberately supports the recurrence subset the canonical recurrence owner can
represent (DAILY/WEEKLY, one DTSTART weekday). Unsupported rules fail the whole snapshot;
they are never approximated into misleading classes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar

from student_execution_os.domain.model import ObligationCategory
from student_execution_os.recurrence.source import (
    SourceEvent,
    SourceOccurrenceChange,
    SourceSeries,
    SourceSnapshot,
)

from .model import AcademicProviderError, AcademicProviderResult

MAX_ICS_BYTES = 5 * 1024 * 1024
_TEACHER_LINE = re.compile(r"(?im)^\s*(?:teacher|lecturer|преподаватель)\s*:\s*(.+?)\s*$")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _text(component, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sequence(component) -> int:
    value = component.get("SEQUENCE")
    try:
        sequence = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise AcademicProviderError("calendar SEQUENCE is invalid", "PARSE_ERROR") from exc
    if sequence < 0:
        raise AcademicProviderError("calendar SEQUENCE is invalid", "PARSE_ERROR")
    return sequence


def _updated(component) -> datetime | None:
    for name in ("LAST-MODIFIED", "DTSTAMP"):
        value = component.get(name)
        if value is None:
            continue
        try:
            decoded = component.decoded(name)
        except (ValueError, TypeError) as exc:
            raise AcademicProviderError(f"calendar {name} is invalid", "PARSE_ERROR") from exc
        if isinstance(decoded, datetime):
            return (decoded.replace(tzinfo=timezone.utc) if decoded.tzinfo is None
                    else decoded.astimezone(timezone.utc))
    return None


def _tz_name(component, name: str, value: datetime, default_timezone: str) -> str:
    prop = component.get(name)
    parameter = None if prop is None else prop.params.get("TZID")
    if parameter:
        candidate = str(parameter)
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError as exc:
            raise AcademicProviderError("calendar uses an unknown TZID", "UNKNOWN_TIMEZONE") from exc
    key = getattr(value.tzinfo, "key", None)
    if key:
        return str(key)
    if value.tzinfo is timezone.utc or (value.tzinfo is not None and value.utcoffset() == timedelta(0)):
        return "UTC"
    return default_timezone


@dataclass(frozen=True)
class _Moment:
    aware: datetime
    local: datetime
    timezone_name: str
    all_day: bool


def _moment(component, name: str, default_timezone: str) -> _Moment:
    try:
        decoded = component.decoded(name)
    except (KeyError, ValueError, TypeError) as exc:
        raise AcademicProviderError(f"calendar {name} is missing or invalid", "PARSE_ERROR") from exc
    if isinstance(decoded, datetime):
        zone_name = _tz_name(component, name, decoded, default_timezone)
        zone = ZoneInfo(zone_name)
        aware = decoded.replace(tzinfo=zone) if decoded.tzinfo is None else decoded.astimezone(zone)
        return _Moment(aware=aware, local=aware.replace(tzinfo=None), timezone_name=zone_name, all_day=False)
    if isinstance(decoded, date):
        zone = ZoneInfo(default_timezone)
        local = datetime.combine(decoded, time.min)
        return _Moment(aware=local.replace(tzinfo=zone), local=local,
                       timezone_name=default_timezone, all_day=True)
    raise AcademicProviderError(f"calendar {name} has an unsupported value", "PARSE_ERROR")


def _duration(component, start: _Moment, default_timezone: str) -> tuple[int, datetime]:
    if component.get("DTEND") is not None:
        end = _moment(component, "DTEND", start.timezone_name or default_timezone)
        minutes = int((
            end.aware.astimezone(timezone.utc) - start.aware.astimezone(timezone.utc)
        ).total_seconds() // 60)
        end_aware = end.aware
    elif component.get("DURATION") is not None:
        try:
            value = component.decoded("DURATION")
        except (ValueError, TypeError) as exc:
            raise AcademicProviderError("calendar DURATION is invalid", "PARSE_ERROR") from exc
        if not isinstance(value, timedelta):
            raise AcademicProviderError("calendar DURATION is invalid", "PARSE_ERROR")
        minutes = int(value.total_seconds() // 60)
        end_aware = start.aware + value
    elif start.all_day:
        minutes = 24 * 60
        end_aware = start.aware + timedelta(days=1)
    else:
        raise AcademicProviderError("timed event needs DTEND or DURATION", "PARSE_ERROR")
    if minutes <= 0:
        raise AcademicProviderError("calendar event duration must be positive", "PARSE_ERROR")
    return minutes, end_aware


def _category(component) -> ObligationCategory:
    raw = component.get("CATEGORIES")
    text = "" if raw is None else str(raw).upper()
    summary = (_text(component, "SUMMARY") or "").upper()
    if "EXAM" in text or "ЭКЗАМ" in summary:
        return ObligationCategory.EXAM
    return ObligationCategory.LESSON


def _teacher(component, description: str | None, properties: tuple[str, ...]) -> str | None:
    for name in properties:
        value = _text(component, name)
        if value:
            return value
    match = _TEACHER_LINE.search(description or "")
    return None if match is None else match.group(1).strip()


def _rule(component, start: _Moment) -> str:
    raw = component.get("RRULE")
    if raw is None:
        raise AcademicProviderError("recurring event has no RRULE", "PARSE_ERROR")
    values = {str(key).upper(): list(value) for key, value in raw.items()}
    unsupported = set(values) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}
    if unsupported:
        raise AcademicProviderError(
            "calendar RRULE is outside the supported academic subset: " + ",".join(sorted(unsupported)),
            "UNSUPPORTED_RRULE",
        )
    freq = str(values.get("FREQ", [""])[0]).upper()
    if freq not in {"DAILY", "WEEKLY"}:
        raise AcademicProviderError("only DAILY/WEEKLY academic RRULEs are supported", "UNSUPPORTED_RRULE")
    byday = [str(value).upper() for value in values.get("BYDAY", [])]
    if byday and (len(byday) != 1 or byday[0] != _WEEKDAYS[start.local.weekday()]):
        raise AcademicProviderError(
            "multi-day or shifted BYDAY rules must be separate calendar series",
            "UNSUPPORTED_RRULE",
        )
    try:
        interval = int(values.get("INTERVAL", [1])[0])
    except (TypeError, ValueError) as exc:
        raise AcademicProviderError("calendar RRULE interval is invalid", "PARSE_ERROR") from exc
    if interval < 1:
        raise AcademicProviderError("calendar RRULE interval is invalid", "PARSE_ERROR")
    parts = [f"FREQ={freq}"]
    if interval != 1:
        parts.append(f"INTERVAL={interval}")
    if values.get("COUNT"):
        try:
            count = int(values["COUNT"][0])
        except (TypeError, ValueError) as exc:
            raise AcademicProviderError("calendar RRULE count is invalid", "PARSE_ERROR") from exc
        if count < 1:
            raise AcademicProviderError("calendar RRULE count is invalid", "PARSE_ERROR")
        parts.append(f"COUNT={count}")
    if values.get("UNTIL"):
        until = values["UNTIL"][0]
        if isinstance(until, date) and not isinstance(until, datetime):
            local = datetime.combine(until, time.max).replace(microsecond=0)
        elif isinstance(until, datetime):
            zone = ZoneInfo(start.timezone_name)
            local = (until.replace(tzinfo=zone) if until.tzinfo is None else until.astimezone(zone)).replace(tzinfo=None)
        else:
            raise AcademicProviderError("calendar RRULE UNTIL is invalid", "PARSE_ERROR")
        parts.append(f"UNTIL={local.isoformat()}")
    return ";".join(parts)


def _exdates(component, timezone_name: str) -> tuple[datetime, ...]:
    props = component.get("EXDATE")
    if props is None:
        return ()
    items = props if isinstance(props, list) else [props]
    values: list[datetime] = []
    zone = ZoneInfo(timezone_name)
    for prop in items:
        for decoded in prop.dts:
            value = decoded.dt
            if isinstance(value, date) and not isinstance(value, datetime):
                value = datetime.combine(value, time.min)
            if value.tzinfo is not None:
                value = value.astimezone(zone).replace(tzinfo=None)
            values.append(value)
    return tuple(sorted(set(values)))


def _fingerprint(component) -> str:
    return hashlib.sha256(component.to_ical()).hexdigest()


def _newer(left, right):
    lk = (_sequence(left), _updated(left) or datetime.min.replace(tzinfo=timezone.utc))
    rk = (_sequence(right), _updated(right) or datetime.min.replace(tzinfo=timezone.utc))
    if lk != rk:
        return left if lk > rk else right
    if _fingerprint(left) != _fingerprint(right):
        raise AcademicProviderError("calendar contains conflicting duplicate revisions", "CONFLICTING_DUPLICATE")
    return left


def _select_components(components: Iterable[Any], default_timezone: str) -> dict[tuple[str, str], Any]:
    selected: dict[tuple[str, str], Any] = {}
    for component in components:
        uid = _text(component, "UID")
        if not uid:
            raise AcademicProviderError("calendar VEVENT has no UID", "PARSE_ERROR")
        rid = ""
        if component.get("RECURRENCE-ID") is not None:
            rid = _moment(component, "RECURRENCE-ID", default_timezone).local.isoformat()
        key = (uid, rid)
        selected[key] = component if key not in selected else _newer(selected[key], component)
    return selected


def parse_icalendar(
    content: bytes,
    *,
    source_system_id: str,
    default_timezone: str,
    complete: bool = True,
    teacher_properties: tuple[str, ...] = ("X-TEACHER", "X-HSE-TEACHER", "X-HSE-LECTURER"),
) -> AcademicProviderResult:
    if not source_system_id:
        raise AcademicProviderError("academic source identity is required", "CONFIGURATION")
    if len(content) > MAX_ICS_BYTES:
        raise AcademicProviderError("calendar file is too large", "TOO_LARGE")
    try:
        ZoneInfo(default_timezone)
    except ZoneInfoNotFoundError as exc:
        raise AcademicProviderError("default timezone is unknown", "UNKNOWN_TIMEZONE") from exc
    try:
        calendar = Calendar.from_ical(content)
    except Exception as exc:  # parser exceptions are intentionally not leaked to the UI
        raise AcademicProviderError("calendar file could not be parsed", "PARSE_ERROR") from exc
    if str(calendar.get("VERSION") or "") != "2.0":
        raise AcademicProviderError("calendar must be iCalendar 2.0", "PARSE_ERROR")
    components = [component for component in calendar.walk("VEVENT")]
    selected = _select_components(components, default_timezone)
    method_cancel = str(calendar.get("METHOD") or "").upper() == "CANCEL"
    series: list[SourceSeries] = []
    events: list[SourceEvent] = []
    diagnostics: list[str] = []

    for (uid, rid), master in sorted(selected.items()):
        if rid or master.get("RRULE") is None:
            continue
        if master.get("RDATE") is not None:
            raise AcademicProviderError("RDATE academic series are not yet supported", "UNSUPPORTED_RRULE")
        if method_cancel or str(master.get("STATUS") or "").upper() == "CANCELLED":
            diagnostics.append(f"cancelled-series:{uid}")
            continue
        start = _moment(master, "DTSTART", default_timezone)
        duration, _ = _duration(master, start, default_timezone)
        description = _text(master, "DESCRIPTION")
        changes: list[SourceOccurrenceChange] = []
        for (child_uid, child_rid), child in sorted(selected.items()):
            if child_uid != uid or not child_rid:
                continue
            original = _moment(child, "RECURRENCE-ID", start.timezone_name).local
            cancelled = method_cancel or str(child.get("STATUS") or "").upper() == "CANCELLED"
            child_start = None if child.get("DTSTART") is None else _moment(child, "DTSTART", start.timezone_name)
            child_starts_local = (
                None
                if child_start is None
                else child_start.aware.astimezone(ZoneInfo(start.timezone_name)).replace(tzinfo=None)
            )
            child_duration = None
            if child_start is not None and (child.get("DTEND") is not None or child.get("DURATION") is not None):
                child_duration, _ = _duration(child, child_start, start.timezone_name)
            child_description = _text(child, "DESCRIPTION")
            changes.append(SourceOccurrenceChange(
                recurrence_local=original,
                cancelled=cancelled,
                starts_local=child_starts_local,
                duration_minutes=child_duration,
                title=_text(child, "SUMMARY"),
                location_text=_text(child, "LOCATION"),
                teacher=_teacher(child, child_description, teacher_properties),
                sequence=_sequence(child),
                updated_at=_updated(child),
            ))
        series.append(SourceSeries(
            uid=uid,
            title=_text(master, "SUMMARY") or "Занятие",
            dtstart_local=start.local,
            duration_minutes=duration,
            recurrence_rule=_rule(master, start),
            timezone_name=start.timezone_name,
            category=_category(master),
            description=description,
            location_text=_text(master, "LOCATION"),
            teacher=_teacher(master, description, teacher_properties),
            exdates_local=_exdates(master, start.timezone_name),
            changes=tuple(changes),
            sequence=_sequence(master),
            updated_at=_updated(master),
        ))

    series_uids = {item.uid for item in series}
    for (uid, rid), component in sorted(selected.items()):
        if not rid and component.get("RRULE") is not None:
            continue
        if rid and uid in series_uids:
            continue
        cancelled = method_cancel or str(component.get("STATUS") or "").upper() == "CANCELLED"
        if component.get("DTSTART") is None:
            diagnostics.append(f"ignored-detached-tombstone:{uid}#{rid}")
            continue
        start = _moment(component, "DTSTART", default_timezone)
        _, end = _duration(component, start, default_timezone)
        description = _text(component, "DESCRIPTION")
        event_uid = uid if not rid else f"{uid}::RECURRENCE-ID={rid}"
        events.append(SourceEvent(
            uid=event_uid,
            title=_text(component, "SUMMARY") or "Занятие",
            starts_at=start.aware,
            ends_at=end,
            category=_category(component),
            description=description,
            location_text=_text(component, "LOCATION"),
            teacher=_teacher(component, description, teacher_properties),
            cancelled=cancelled,
            sequence=_sequence(component),
            updated_at=_updated(component),
        ))

    digest = hashlib.sha256(content).hexdigest()
    return AcademicProviderResult(
        snapshot=SourceSnapshot(
            source_system_id=source_system_id,
            series=tuple(sorted(series, key=lambda item: item.uid)),
            events=tuple(sorted(events, key=lambda item: item.uid)),
            complete=complete,
        ),
        content_sha256=digest,
        component_count=len(components),
        diagnostics=tuple(diagnostics),
    )


class ICalendarAcademicProvider:
    provider_id = "ICALENDAR"

    def __init__(
        self,
        *,
        source_system_id: str,
        default_timezone: str,
        reader: Callable[[], bytes],
        complete: bool = True,
    ) -> None:
        self.source_system_id = source_system_id
        self.default_timezone = default_timezone
        self.reader = reader
        self.complete = complete

    def fetch(self) -> AcademicProviderResult:
        content = self.reader()
        if not isinstance(content, bytes):
            raise AcademicProviderError("calendar reader did not return bytes", "PROVIDER_PROTOCOL_ERROR")
        return parse_icalendar(
            content,
            source_system_id=self.source_system_id,
            default_timezone=self.default_timezone,
            complete=self.complete,
        )
