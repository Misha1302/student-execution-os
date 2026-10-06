"""Pure expansion of a recurrence rule into original local occurrence times.

One iterator shared by the owners that materialize occurrences from a local civil
DTSTART (check-ins, reminder series). Identity is the *original* local start
(``recurrence_id``): resolving it to an instant (DST gap/fold) or moving one
occurrence never changes which occurrence it is.

Supported: ``DAILY`` / ``WEEKLY`` with ``INTERVAL``, ``COUNT``, ``UNTIL`` and, for
``WEEKLY``, ``BYDAY`` (weeks start on Monday; the first week is DTSTART's week, and
weekdays before DTSTART in that week are not occurrences).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

from student_execution_os.domain.errors import ValidationError

from .model import RecurrenceFrequency, RecurrenceRule

# A bound on how many occurrences a single scan may visit (≈ 27 years of daily items).
MAX_SCAN = 10_000


def _check(dtstart_local: datetime) -> None:
    if dtstart_local.tzinfo is not None:
        raise ValidationError("recurrence DTSTART must be naive local civil time")


def _bounded(candidate: datetime, rule: RecurrenceRule, series_end_before: datetime | None) -> bool:
    if series_end_before is not None and candidate >= series_end_before:
        return False
    if rule.until_local is not None and candidate > rule.until_local:
        return False
    return True


def iter_original_locals(
    dtstart_local: datetime,
    rule: RecurrenceRule,
    *,
    series_end_before: datetime | None = None,
    start_local: datetime | None = None,
) -> Iterator[datetime]:
    """Yield original local starts in order, beginning at or after ``start_local``.

    ``COUNT`` always counts from DTSTART, so skipping ahead never changes which
    occurrences exist.
    """
    _check(dtstart_local)
    if rule.by_weekdays is None:
        step = timedelta(days=rule.interval if rule.frequency is RecurrenceFrequency.DAILY else 7 * rule.interval)
        index = 0
        if start_local is not None and start_local > dtstart_local:
            index = max(0, (start_local - dtstart_local) // step - 1)
        while True:
            if rule.count is not None and index >= rule.count:
                return
            candidate = dtstart_local + index * step
            if not _bounded(candidate, rule, series_end_before):
                return
            if start_local is None or candidate >= start_local:
                yield candidate
            index += 1
    week_start = (dtstart_local - timedelta(days=dtstart_local.weekday())).replace(
        hour=dtstart_local.hour, minute=dtstart_local.minute, second=0, microsecond=0)
    week = 0
    seen = 0
    if start_local is not None and rule.count is None and start_local > dtstart_local:
        # Without COUNT the position is free to compute: jump to the right week.
        weeks_ahead = max(0, (start_local - week_start).days // 7 - 1)
        week = weeks_ahead - weeks_ahead % rule.interval
    for _ in range(MAX_SCAN):
        base = week_start + timedelta(weeks=week)
        for day in rule.by_weekdays:
            candidate = base + timedelta(days=day)
            if candidate < dtstart_local:
                continue
            if rule.count is not None and seen >= rule.count:
                return
            if not _bounded(candidate, rule, series_end_before):
                return
            seen += 1
            if start_local is None or candidate >= start_local:
                yield candidate
        week += rule.interval
    raise ValidationError("recurrence expansion exceeded its scan bound")


def contains_original(
    dtstart_local: datetime,
    rule: RecurrenceRule,
    original_local: datetime,
    *,
    series_end_before: datetime | None = None,
) -> bool:
    """Whether ``original_local`` is exactly one occurrence identity of the rule."""
    _check(dtstart_local)
    if original_local.tzinfo is not None or original_local < dtstart_local:
        return False
    for candidate in iter_original_locals(dtstart_local, rule, series_end_before=series_end_before,
                                          start_local=original_local):
        return candidate == original_local
    return False


def remaining_count(dtstart_local: datetime, rule: RecurrenceRule, boundary_local: datetime) -> int | None:
    """COUNT left for a successor series that starts at ``boundary_local`` (None = unbounded)."""
    if rule.count is None:
        return None
    before = 0
    for candidate in iter_original_locals(dtstart_local, rule):
        if candidate >= boundary_local:
            break
        before += 1
    left = rule.count - before
    if left < 1:
        raise ValidationError("split boundary is beyond the COUNT of the series")
    return left
