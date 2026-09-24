from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import time, timedelta
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.planning.model import FeasibilityStatus

from .delivery import DeliveryReceipt
from .model import NotificationKind, QuietHours
from .repository import SQLiteNotificationRepository


ALL_KINDS = {kind.value for kind in NotificationKind}
DEFAULT_ENABLED = {
    NotificationKind.DEADLINE_WARNING.value,
    NotificationKind.LATEST_SAFE_START.value,
    NotificationKind.LATEST_SAFE_DEPARTURE.value,
    NotificationKind.RISK_THRESHOLD.value,
    NotificationKind.PLAN_CONFLICT.value,
    NotificationKind.COMPLETION_FOLLOWUP.value,
    NotificationKind.PREPARATION.value,
}
DEFAULT_LEADS = {kind: 0 for kind in ALL_KINDS} | {NotificationKind.DEADLINE_WARNING.value: 1440}


def _clock(value: str) -> time:
    try:
        result = time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("quiet hour must be HH:MM") from exc
    if result.second or result.microsecond:
        raise ValidationError("quiet hours use minute precision")
    return result


@dataclass(frozen=True)
class NotificationPreferences:
    timezone_name: str
    quiet_starts_local: str
    quiet_ends_local: str
    lead_times: dict[str, int]
    enabled_kinds: tuple[str, ...]
    grouping_minutes: int
    cooldown_minutes: int
    version: int

    @property
    def quiet_hours(self) -> QuietHours:
        return QuietHours(self.timezone_name, _clock(self.quiet_starts_local), _clock(self.quiet_ends_local))

    def payload(self) -> dict[str, object]:
        return {
            "timezone": self.timezone_name,
            "quiet_hours": {"starts_local": self.quiet_starts_local, "ends_local": self.quiet_ends_local},
            "lead_times_minutes": self.lead_times,
            "enabled_kinds": list(self.enabled_kinds),
            "grouping_minutes": self.grouping_minutes,
            "cooldown_minutes": self.cooldown_minutes,
            "version": self.version,
        }


class SQLiteNotificationPreferencesRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def get(self, account_id: str) -> NotificationPreferences:
        self.canonical._require_account(account_id)
        row = self.canonical.connection.execute(
            "SELECT * FROM notification_preferences WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None:
            now = self.canonical.clock.now()
            with self.canonical._tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO notification_preferences(account_id,lead_times_json,enabled_kinds_json,updated_at) VALUES (?,?,?,?)",
                    (account_id, json.dumps(DEFAULT_LEADS, sort_keys=True), json.dumps(sorted(DEFAULT_ENABLED)), _iso(now)),
                )
            row = self.canonical.connection.execute(
                "SELECT * FROM notification_preferences WHERE account_id=?", (account_id,)
            ).fetchone()
        return NotificationPreferences(
            timezone_name=row["timezone_name"], quiet_starts_local=row["quiet_starts_local"],
            quiet_ends_local=row["quiet_ends_local"], lead_times=json.loads(row["lead_times_json"]),
            enabled_kinds=tuple(json.loads(row["enabled_kinds_json"])),
            grouping_minutes=int(row["grouping_minutes"]), cooldown_minutes=int(row["cooldown_minutes"]),
            version=int(row["version"]),
        )

    def update(self, account_id: str, payload: dict[str, object]) -> NotificationPreferences:
        current = self.get(account_id)
        expected = int(payload["expected_version"])
        if expected != current.version:
            raise VersionConflict("notification preferences version changed")
        zone = str(payload.get("timezone", current.timezone_name))
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("unknown IANA timezone") from exc
        quiet = payload.get("quiet_hours", {})
        if not isinstance(quiet, dict):
            raise ValidationError("quiet_hours must be an object")
        starts = str(quiet.get("starts_local", current.quiet_starts_local))
        ends = str(quiet.get("ends_local", current.quiet_ends_local))
        QuietHours(zone, _clock(starts), _clock(ends))
        leads = payload.get("lead_times_minutes", current.lead_times)
        if not isinstance(leads, dict) or not set(leads).issubset(ALL_KINDS):
            raise ValidationError("lead_times_minutes contains an unknown notification kind")
        leads = {str(key): int(value) for key, value in leads.items()}
        if any(value < 0 or value > 43200 for value in leads.values()):
            raise ValidationError("notification lead time is outside 0-43200 minutes")
        enabled = payload.get("enabled_kinds", current.enabled_kinds)
        if not isinstance(enabled, (list, tuple)) or not set(enabled).issubset(ALL_KINDS):
            raise ValidationError("enabled_kinds contains an unknown notification kind")
        grouping = int(payload.get("grouping_minutes", current.grouping_minutes))
        cooldown = int(payload.get("cooldown_minutes", current.cooldown_minutes))
        if min(grouping, cooldown) < 0:
            raise ValidationError("grouping/cooldown cannot be negative")
        now = self.canonical.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE notification_preferences SET timezone_name=?,quiet_starts_local=?,quiet_ends_local=?,"
                "lead_times_json=?,enabled_kinds_json=?,grouping_minutes=?,cooldown_minutes=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND version=?",
                (zone, starts, ends, json.dumps(leads, sort_keys=True), json.dumps(sorted(enabled)), grouping,
                 cooldown, _iso(now), account_id, expected),
            )
            if cur.rowcount != 1:
                raise VersionConflict("notification preferences changed before commit")
        return self.get(account_id)


class NotificationPolicyEngine:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.notifications = SQLiteNotificationRepository(canonical)

    def reconcile(self, account_id: str, snapshot, outcome) -> list[object]:
        prefs = SQLiteNotificationPreferencesRepository(self.canonical).get(account_id)
        revision = self.canonical.get_server_revision(account_id)
        enabled = set(prefs.enabled_kinds)
        created = []

        def schedule(kind: NotificationKind, entity: str | None, token: str, when, group: str) -> None:
            if kind.value not in enabled or when is None:
                return
            lead = prefs.lead_times.get(kind.value, 0)
            when = when - timedelta(minutes=lead)
            key = self.notifications.transition_suppression_key(kind=kind, entity_ref=entity, transition_token=token)
            created.append(self.notifications.schedule(
                account_id=account_id, suppression_key=key, kind=kind, entity_ref=entity,
                scheduled_for=when, domain_revision=revision,
                plan_id=outcome.plan.id, plan_revision=outcome.plan.plan_revision,
                group_key=group, cooldown_until=when + timedelta(minutes=prefs.cooldown_minutes),
                quiet_hours=prefs.quiet_hours,
            ))

        risk_by_task = {risk.task_id: risk for risk in outcome.risks}
        for task in snapshot.tasks:
            task_id = task.obligation.id
            if task.actual_cutoff.at is not None:
                schedule(NotificationKind.DEADLINE_WARNING, task_id, task.actual_cutoff.at.isoformat(), task.actual_cutoff.at, f"task:{task_id}")
            risk = risk_by_task.get(task_id)
            if risk is not None:
                schedule(NotificationKind.LATEST_SAFE_START, task_id, outcome.plan.plan_revision, risk.latest_safe_start, f"task:{task_id}")
                if risk.state.value in {"AT_RISK", "CRITICAL", "IMPOSSIBLE", "OVERDUE"}:
                    schedule(NotificationKind.RISK_THRESHOLD, task_id, f"{risk.state.value}:{outcome.plan.plan_revision}", self.canonical.clock.now(), f"task:{task_id}")
        for transition in snapshot.travel_projection.transitions:
            schedule(NotificationKind.LATEST_SAFE_DEPARTURE, transition.target_event_id, outcome.plan.plan_revision,
                     transition.latest_safe_departure, f"event:{transition.target_event_id}")
        for dependency in snapshot.dependencies:
            if dependency.successor_kind.value == "EVENT":
                event = next((item for item in snapshot.events if item.obligation.id == dependency.successor_id), None)
                if event is not None:
                    schedule(NotificationKind.PREPARATION, dependency.predecessor_task_id,
                             f"{dependency.id}:{outcome.plan.plan_revision}", event.interval.starts_at,
                             f"event:{dependency.successor_id}")
        if outcome.plan.feasibility_status is not FeasibilityStatus.FEASIBLE:
            schedule(NotificationKind.PLAN_CONFLICT, None, outcome.plan.plan_revision, self.canonical.clock.now(), "plan")
        return created


@dataclass(frozen=True)
class FCMConfig:
    endpoint: str | None = None
    bearer_token: str | None = None

    @classmethod
    def from_environment(cls) -> "FCMConfig":
        return cls(os.environ.get("SEOS_FCM_ENDPOINT"), os.environ.get("SEOS_FCM_BEARER_TOKEN"))

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.bearer_token)


class FCMChannel:
    """Provider adapter boundary. The stable delivery key is always forwarded."""

    def __init__(self, config: FCMConfig | None = None, transport: Callable[..., DeliveryReceipt] | None = None,
                 canonical: SQLiteCanonicalRepository | None = None) -> None:
        self.config = config or FCMConfig.from_environment()
        self.transport = transport
        self.canonical = canonical

    def send(self, notification, delivery_key: str) -> DeliveryReceipt:
        if not self.config.configured:
            return DeliveryReceipt(False, error="FCM_UNCONFIGURED")
        if self.transport is not None:
            return self.transport(notification=notification, delivery_key=delivery_key, config=self.config)
        if self.canonical is None:
            return DeliveryReceipt(False, error="FCM_TRANSPORT_UNCONFIGURED")
        devices = self.canonical.connection.execute(
            "SELECT id,token FROM mobile_devices WHERE account_id=? AND active=1 AND token<>'' ORDER BY id",
            (notification.account_id,),
        ).fetchall()
        if not devices:
            return DeliveryReceipt(False, error="NO_ACTIVE_MOBILE_DEVICE")
        provider_ids = []
        for device in devices:
            device_key = f"{delivery_key}:{device['id']}"
            deep_link = (
                f"#/calendar?event={notification.entity_ref}"
                if notification.group_key and notification.group_key.startswith("event:")
                else f"#/task/{notification.entity_ref}"
                if notification.entity_ref
                else "#/today"
            )
            body = json.dumps({"message": {
                "token": device["token"],
                "notification": {"title": "Student Execution OS", "body": notification.kind.value.replace("_", " ").title()},
                "data": {
                    "notification_id": notification.id,
                    "kind": notification.kind.value,
                    "delivery_key": device_key,
                    "deep_link": deep_link,
                },
            }}).encode()
            request = urllib.request.Request(
                self.config.endpoint,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.config.bearer_token}",
                    "Content-Type": "application/json",
                    "X-Idempotency-Key": device_key,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    response_data = json.loads(response.read().decode() or "{}")
                    provider_ids.append(str(response_data.get("name") or device_key))
            except urllib.error.HTTPError as exc:
                return DeliveryReceipt(False, error=f"FCM_HTTP_{exc.code}")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                return DeliveryReceipt(False, error=f"FCM_CHANNEL_ERROR:{type(exc).__name__}")
        return DeliveryReceipt(True, provider_message_id=",".join(provider_ids))


class NotificationWorker:
    def __init__(self, canonical: SQLiteCanonicalRepository, sender: Callable) -> None:
        self.canonical = canonical
        self.repository = SQLiteNotificationRepository(canonical)
        self.sender = sender

    def run_once(self, account_id: str, worker_id: str = "notification-worker") -> int:
        delivered = 0
        for item in self.repository.due(account_id):
            result = self.repository.deliver(
                account_id=account_id, notification_id=item.id, worker_id=worker_id, sender=self.sender
            )
            delivered += int(result.state.value == "DELIVERED")
        return delivered


def register_device(canonical: SQLiteCanonicalRepository, account_id: str, payload: dict[str, object]) -> dict[str, object]:
    canonical._require_account(account_id)
    token = str(payload.get("token", "")).strip()
    if not token or len(token) > 4096:
        raise ValidationError("a valid push token is required")
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = canonical.clock.now()
    existing = canonical.connection.execute(
        "SELECT * FROM mobile_devices WHERE account_id=? AND token_hash=?", (account_id, digest)
    ).fetchone()
    with canonical._tx() as conn:
        if existing is None:
            device_id = str(payload.get("device_id") or uuid4())
            conn.execute(
                "INSERT INTO mobile_devices(id,account_id,platform,token_hash,token,label,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (device_id, account_id, "ANDROID", digest, token, payload.get("label"), _iso(now), _iso(now)),
            )
        else:
            device_id = existing["id"]
            conn.execute(
                "UPDATE mobile_devices SET token=?,label=?,active=1,version=version+1,updated_at=? WHERE account_id=? AND id=?",
                (token, payload.get("label"), _iso(now), account_id, device_id),
            )
    row = canonical.connection.execute(
        "SELECT id,platform,label,active,version,created_at,updated_at FROM mobile_devices WHERE account_id=? AND id=?",
        (account_id, device_id),
    ).fetchone()
    return dict(row)
