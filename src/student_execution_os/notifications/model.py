from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import require_aware


class NotificationKind(StrEnum):
    LATEST_SAFE_DEPARTURE = "LATEST_SAFE_DEPARTURE"
    LATEST_SAFE_START = "LATEST_SAFE_START"
    RISK_THRESHOLD = "RISK_THRESHOLD"
    DEADLINE_WARNING = "DEADLINE_WARNING"
    PLAN_CONFLICT = "PLAN_CONFLICT"
    SOURCE_CHANGE = "SOURCE_CHANGE"
    COMPLETION_FOLLOWUP = "COMPLETION_FOLLOWUP"


class NotificationState(StrEnum):
    PENDING = "PENDING"
    SNOOZED = "SNOOZED"
    DELIVERED = "DELIVERED"
    SUPPRESSED = "SUPPRESSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class QuietHours:
    timezone_name: str
    starts_local: time
    ends_local: time

    def __post_init__(self) -> None:
        if not self.timezone_name:
            raise ValidationError("quiet-hours timezone is required")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("unknown quiet-hours IANA timezone") from exc
        if self.starts_local == self.ends_local:
            raise ValidationError("quiet hours cannot cover an ambiguous full day")

    def defer(self, when: datetime) -> datetime:
        require_aware(when, "notification time")
        zone = ZoneInfo(self.timezone_name)
        local = when.astimezone(zone)
        current = local.timetz().replace(tzinfo=None)
        overnight = self.starts_local > self.ends_local
        in_quiet = (
            self.starts_local <= current < self.ends_local
            if not overnight
            else current >= self.starts_local or current < self.ends_local
        )
        if not in_quiet:
            return when
        end_date = local.date()
        if overnight and current >= self.starts_local:
            end_date += timedelta(days=1)
        target_naive = datetime.combine(end_date, self.ends_local)
        # Deterministic fold=0; if nonexistent, shift to first valid local minute.
        for offset in range(181):
            probe = target_naive + timedelta(minutes=offset)
            aware = probe.replace(tzinfo=zone, fold=0)
            if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == probe:
                return aware.astimezone(timezone.utc)
        raise ValidationError("could not resolve quiet-hours end through DST gap")


@dataclass(frozen=True)
class Notification:
    id: str
    account_id: str
    suppression_key: str
    kind: NotificationKind
    entity_ref: str | None
    domain_revision: int
    plan_id: str | None
    plan_revision: str | None
    scheduled_for: datetime
    state: NotificationState
    initial_notification_id: str | None
    group_key: str | None
    cooldown_until: datetime | None
    snoozed_until: datetime | None
    delivered_at: datetime | None
    attempt_count: int
    version: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.suppression_key:
            raise ValidationError("notification identity is required")
        if self.domain_revision < 0:
            raise ValidationError("domain revision cannot be negative")
        require_aware(self.scheduled_for, "scheduled_for")
        require_aware(self.cooldown_until, "cooldown_until")
        require_aware(self.snoozed_until, "snoozed_until")
        require_aware(self.delivered_at, "delivered_at")
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if (self.plan_id is None) != (self.plan_revision is None):
            raise ValidationError("plan id and plan revision must be present together")
        if self.attempt_count < 0:
            raise ValidationError("attempt_count cannot be negative")
        if self.version < 1:
            raise ValidationError("notification version must be >= 1")
