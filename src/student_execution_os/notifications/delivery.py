from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import _dt, _iso

from .model import Notification, NotificationState


class DeliveryState(StrEnum):
    READY = "READY"
    LEASED = "LEASED"
    RETRY_WAIT = "RETRY_WAIT"
    SENT = "SENT"
    SUPPRESSED = "SUPPRESSED"
    DEAD = "DEAD"


@dataclass(frozen=True)
class NotificationDeliveryPolicy:
    lease_seconds: int = 60
    base_retry_seconds: int = 30
    max_retry_seconds: int = 1800
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValidationError("delivery lease_seconds must be > 0")
        if self.base_retry_seconds <= 0 or self.max_retry_seconds < self.base_retry_seconds:
            raise ValidationError("invalid delivery retry policy")
        if self.max_attempts <= 0:
            raise ValidationError("delivery max_attempts must be > 0")

    def retry_delay(self, attempt_count: int) -> timedelta:
        seconds = min(
            self.max_retry_seconds,
            self.base_retry_seconds * (2 ** max(0, attempt_count - 1)),
        )
        return timedelta(seconds=seconds)


@dataclass(frozen=True)
class NotificationDelivery:
    notification_id: str
    account_id: str
    delivery_key: str
    state: DeliveryState
    lease_owner: str | None
    lease_expires_at: datetime | None
    next_attempt_at: datetime
    attempt_count: int
    provider_message_id: str | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DeliveryLease:
    notification: Notification
    delivery: NotificationDelivery


@dataclass(frozen=True)
class DeliveryReceipt:
    delivered: bool
    provider_message_id: str | None = None
    error: str | None = None


class SQLiteNotificationDeliveryOutbox:
    """Durable notification sender boundary.

    A lease is committed before the external channel call. If a worker crashes
    after the provider accepted the message but before local success is recorded,
    the expired lease is reclaimed with the same delivery_key. Channels MUST use
    that stable key for provider-side idempotency when they support it.
    """

    def __init__(self, notifications, *, policy: NotificationDeliveryPolicy | None = None) -> None:
        self.notifications = notifications
        self.canonical = notifications.canonical
        self.connection = notifications.connection
        self.clock = notifications.clock
        self.policy = policy or NotificationDeliveryPolicy()

    @staticmethod
    def stable_delivery_key(notification: Notification) -> str:
        raw = (
            f"{notification.account_id}\0{notification.id}\0"
            f"{notification.suppression_key}\0notification-delivery-v1"
        )
        return "delivery-" + hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, account_id: str, notification_id: str) -> NotificationDelivery:
        row = self.connection.execute(
            "SELECT * FROM notification_delivery_outbox WHERE account_id=? AND notification_id=?",
            (account_id, notification_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("notification delivery not found")
        return self._from_row(row)

    def ensure(self, notification: Notification) -> NotificationDelivery:
        delivery_key = self.stable_delivery_key(notification)
        effective_due = notification.snoozed_until or notification.scheduled_for
        if notification.cooldown_until is not None and notification.cooldown_until > effective_due:
            effective_due = notification.cooldown_until
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notification_delivery_outbox("
                "notification_id,account_id,delivery_key,state,lease_owner,lease_expires_at,"
                "next_attempt_at,attempt_count,provider_message_id,last_error,version,created_at,updated_at"
                ") VALUES (?,?,?,'READY',NULL,NULL,?,0,NULL,NULL,1,?,?)",
                (
                    notification.id,
                    notification.account_id,
                    delivery_key,
                    _iso(effective_due),
                    _iso(now),
                    _iso(now),
                ),
            )
        delivery = self.get(notification.account_id, notification.id)
        if delivery.delivery_key != delivery_key:
            raise ValidationError("notification delivery key mismatch")
        return delivery

    def claim(self, *, account_id: str, notification_id: str, worker_id: str) -> DeliveryLease | None:
        if not worker_id:
            raise ValidationError("delivery worker_id is required")
        notification = self.notifications.get(account_id, notification_id)
        delivery = self.ensure(notification)
        if notification.state is NotificationState.DELIVERED:
            self._terminalize(delivery, DeliveryState.SENT, None)
            return None
        if notification.state is NotificationState.SUPPRESSED:
            self._terminalize(delivery, DeliveryState.SUPPRESSED, notification.last_error)
            return None

        now = self.clock.now()
        effective_due = notification.snoozed_until or notification.scheduled_for
        if notification.cooldown_until is not None and notification.cooldown_until > effective_due:
            effective_due = notification.cooldown_until
        if effective_due > now or delivery.next_attempt_at > now:
            return None
        if delivery.state is DeliveryState.LEASED and delivery.lease_expires_at and delivery.lease_expires_at > now:
            return None
        if delivery.state in (DeliveryState.SENT, DeliveryState.SUPPRESSED, DeliveryState.DEAD):
            return None

        stale_reason = self.notifications._stale_reason(notification)
        if stale_reason is not None:
            self.notifications._mark_suppressed(notification, stale_reason)
            self._terminalize(self.get(account_id, notification_id), DeliveryState.SUPPRESSED, stale_reason)
            return None
        if notification.initial_notification_id is not None:
            initial = self.notifications.get(account_id, notification.initial_notification_id)
            if initial.state is not NotificationState.DELIVERED:
                reason = "INITIAL_NOTIFICATION_NOT_DELIVERED"
                self.notifications._mark_suppressed(notification, reason)
                self._terminalize(self.get(account_id, notification_id), DeliveryState.SUPPRESSED, reason)
                return None

        lease_expires_at = now + timedelta(seconds=self.policy.lease_seconds)
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE notification_delivery_outbox SET state='LEASED',lease_owner=?,lease_expires_at=?,"
                "attempt_count=attempt_count+1,last_error=NULL,version=version+1,updated_at=? "
                "WHERE account_id=? AND notification_id=? AND version=?",
                (worker_id, _iso(lease_expires_at), _iso(now), account_id, notification_id, delivery.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("notification delivery changed before lease commit")
            ncur = conn.execute(
                "UPDATE notifications SET attempt_count=attempt_count+1,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_iso(now), account_id, notification_id, notification.version),
            )
            if ncur.rowcount != 1:
                raise VersionConflict("notification changed before delivery lease commit")
        return DeliveryLease(
            notification=self.notifications.get(account_id, notification_id),
            delivery=self.get(account_id, notification_id),
        )

    def dispatch(
        self,
        *,
        account_id: str,
        notification_id: str,
        worker_id: str,
        sender: Callable[[Notification, str], DeliveryReceipt | bool],
    ) -> Notification:
        lease = self.claim(account_id=account_id, notification_id=notification_id, worker_id=worker_id)
        if lease is None:
            return self.notifications.get(account_id, notification_id)
        try:
            raw = sender(lease.notification, lease.delivery.delivery_key)
            receipt = raw if isinstance(raw, DeliveryReceipt) else DeliveryReceipt(delivered=bool(raw))
        except Exception as exc:
            receipt = DeliveryReceipt(delivered=False, error=f"CHANNEL_ERROR:{type(exc).__name__}")
        return self.complete(
            account_id=account_id,
            notification_id=notification_id,
            worker_id=worker_id,
            receipt=receipt,
        )

    def complete(
        self,
        *,
        account_id: str,
        notification_id: str,
        worker_id: str,
        receipt: DeliveryReceipt,
    ) -> Notification:
        delivery = self.get(account_id, notification_id)
        if delivery.state is not DeliveryState.LEASED or delivery.lease_owner != worker_id:
            raise ValidationError("notification delivery is not leased by this worker")
        now = self.clock.now()
        notification = self.notifications.get(account_id, notification_id)

        if receipt.delivered:
            with self.canonical._tx() as conn:
                dcur = conn.execute(
                    "UPDATE notification_delivery_outbox SET state='SENT',lease_owner=NULL,lease_expires_at=NULL,"
                    "provider_message_id=?,last_error=NULL,version=version+1,updated_at=? "
                    "WHERE account_id=? AND notification_id=? AND version=? AND lease_owner=?",
                    (receipt.provider_message_id, _iso(now), account_id, notification_id, delivery.version, worker_id),
                )
                if dcur.rowcount != 1:
                    raise VersionConflict("notification delivery changed before success commit")
                ncur = conn.execute(
                    "UPDATE notifications SET state='DELIVERED',delivered_at=?,last_error=NULL,"
                    "version=version+1,updated_at=? WHERE account_id=? AND id=? AND version=?",
                    (_iso(now), _iso(now), account_id, notification_id, notification.version),
                )
                if ncur.rowcount != 1:
                    raise VersionConflict("notification changed before success commit")
            return self.notifications.get(account_id, notification_id)

        error = receipt.error or "CHANNEL_DELIVERY_FAILED"
        dead = delivery.attempt_count >= self.policy.max_attempts
        next_attempt = now + self.policy.retry_delay(delivery.attempt_count)
        with self.canonical._tx() as conn:
            dcur = conn.execute(
                "UPDATE notification_delivery_outbox SET state=?,lease_owner=NULL,lease_expires_at=NULL,"
                "next_attempt_at=?,last_error=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND notification_id=? AND version=? AND lease_owner=?",
                (
                    DeliveryState.DEAD.value if dead else DeliveryState.RETRY_WAIT.value,
                    _iso(next_attempt), error, _iso(now), account_id, notification_id, delivery.version, worker_id,
                ),
            )
            if dcur.rowcount != 1:
                raise VersionConflict("notification delivery changed before retry commit")
            ncur = conn.execute(
                "UPDATE notifications SET state='FAILED',last_error=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (error, _iso(now), account_id, notification_id, notification.version),
            )
            if ncur.rowcount != 1:
                raise VersionConflict("notification changed before retry commit")
        return self.notifications.get(account_id, notification_id)

    def has_active_lease(self, account_id: str, notification_id: str) -> bool:
        row = self.connection.execute(
            "SELECT lease_expires_at FROM notification_delivery_outbox "
            "WHERE account_id=? AND notification_id=? AND state='LEASED'",
            (account_id, notification_id),
        ).fetchone()
        if row is None:
            return False
        expires = _dt(row["lease_expires_at"])
        return expires is not None and expires > self.clock.now()

    def reset_for_recomputed_notification(self, notification: Notification) -> None:
        now = self.clock.now()
        effective_due = notification.snoozed_until or notification.scheduled_for
        if notification.cooldown_until is not None and notification.cooldown_until > effective_due:
            effective_due = notification.cooldown_until
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE notification_delivery_outbox SET state='READY',lease_owner=NULL,lease_expires_at=NULL,"
                "next_attempt_at=?,last_error=NULL,version=version+1,updated_at=? "
                "WHERE account_id=? AND notification_id=? AND state NOT IN ('SENT','SUPPRESSED')",
                (_iso(effective_due), _iso(now), notification.account_id, notification.id),
            )

    def _terminalize(self, delivery: NotificationDelivery, state: DeliveryState, error: str | None) -> None:
        if delivery.state is state:
            return
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE notification_delivery_outbox SET state=?,lease_owner=NULL,lease_expires_at=NULL,"
                "last_error=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND notification_id=? AND version=?",
                (state.value, error, _iso(now), delivery.account_id, delivery.notification_id, delivery.version),
            )

    @staticmethod
    def _from_row(row) -> NotificationDelivery:
        return NotificationDelivery(
            notification_id=row["notification_id"], account_id=row["account_id"],
            delivery_key=row["delivery_key"], state=DeliveryState(row["state"]),
            lease_owner=row["lease_owner"], lease_expires_at=_dt(row["lease_expires_at"]),
            next_attempt_at=_dt(row["next_attempt_at"]), attempt_count=int(row["attempt_count"]),
            provider_message_id=row["provider_message_id"], last_error=row["last_error"],
            version=int(row["version"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )
