from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Callable

from student_execution_os.domain.errors import EntityNotFound, IdempotencyConflict, ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.planning import SQLitePlanStore

from .model import Notification, NotificationKind, NotificationState, QuietHours


class SQLiteNotificationRepository:
    """Workflow-owned notification state.

    Notification writes deliberately do not advance canonical server_revision.
    The revision is captured as justification and revalidated immediately before
    delivery instead of becoming a second owner of Task/Event truth.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    @staticmethod
    def stable_id(account_id: str, suppression_key: str) -> str:
        digest = hashlib.sha256(f"{account_id}\0{suppression_key}".encode()).hexdigest()[:32]
        return f"notif-{digest}"

    @staticmethod
    def transition_suppression_key(
        *, kind: NotificationKind, entity_ref: str | None, transition_token: str
    ) -> str:
        if not transition_token:
            raise ValidationError("transition token is required")
        return f"{kind.value}:{entity_ref or '-'}:{transition_token}"

    def schedule(
        self,
        *,
        account_id: str,
        suppression_key: str,
        kind: NotificationKind,
        scheduled_for: datetime,
        domain_revision: int,
        entity_ref: str | None = None,
        plan_id: str | None = None,
        plan_revision: str | None = None,
        initial_notification_id: str | None = None,
        group_key: str | None = None,
        cooldown_until: datetime | None = None,
        quiet_hours: QuietHours | None = None,
    ) -> Notification:
        self.canonical._require_account(account_id)
        if not suppression_key:
            raise ValidationError("notification suppression_key is required")
        if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
            raise ValidationError("scheduled_for must be offset-aware")
        if (plan_id is None) != (plan_revision is None):
            raise ValidationError("plan id and revision must be supplied together")
        if domain_revision != self.canonical.get_server_revision(account_id):
            raise ValidationError("notification must bind the current domain revision")
        if initial_notification_id is not None:
            # The follow-up may be queued before the initial reminder is delivered,
            # but delivery is fail-closed until the referenced reminder is DELIVERED.
            self.get(account_id, initial_notification_id)
        effective_time = quiet_hours.defer(scheduled_for) if quiet_hours else scheduled_for
        existing = self.get_by_suppression_key(account_id, suppression_key)
        if existing is not None:
            logical = (kind.value, entity_ref, initial_notification_id, group_key)
            existing_logical = (
                existing.kind.value, existing.entity_ref,
                existing.initial_notification_id, existing.group_key,
            )
            if existing_logical != logical:
                raise IdempotencyConflict("notification suppression key reused for different logical notification")
            # One suppression key is one logical transition. Re-computation may
            # refresh the revisions/timing that justify a still-undelivered
            # notification, but it never creates a duplicate or erases a user
            # snooze. Once delivered, that logical transition stays delivered.
            if existing.state is NotificationState.DELIVERED:
                return existing
            next_state = (
                NotificationState.SNOOZED.value
                if existing.state is NotificationState.SNOOZED
                else NotificationState.PENDING.value
            )
            now = self.clock.now()
            with self.canonical._tx() as conn:
                cur = conn.execute(
                    "UPDATE notifications SET domain_revision=?,plan_id=?,plan_revision=?,scheduled_for=?,state=?,"
                    "cooldown_until=?,last_error=NULL,version=version+1,updated_at=? "
                    "WHERE account_id=? AND id=? AND version=?",
                    (
                        domain_revision, plan_id, plan_revision, _iso(effective_time), next_state,
                        _iso(cooldown_until), _iso(now), account_id, existing.id, existing.version,
                    ),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("notification version changed during recomputation")
            return self.get(account_id, existing.id)
        now = self.clock.now()
        notification_id = self.stable_id(account_id, suppression_key)
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO notifications("
                "id,account_id,suppression_key,kind,entity_ref,domain_revision,plan_id,plan_revision,scheduled_for,state,"
                "initial_notification_id,group_key,cooldown_until,snoozed_until,delivered_at,attempt_count,version,last_error,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,'PENDING',?,?,?,?,NULL,0,1,NULL,?,?)",
                (
                    notification_id, account_id, suppression_key, kind.value, entity_ref, domain_revision,
                    plan_id, plan_revision, _iso(effective_time), initial_notification_id, group_key,
                    _iso(cooldown_until), None, _iso(now), _iso(now),
                ),
            )
        return self.get(account_id, notification_id)

    def get(self, account_id: str, notification_id: str) -> Notification:
        row = self.connection.execute(
            "SELECT * FROM notifications WHERE account_id=? AND id=?", (account_id, notification_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("notification not found")
        return self._from_row(row)

    def get_by_suppression_key(self, account_id: str, suppression_key: str) -> Notification | None:
        row = self.connection.execute(
            "SELECT * FROM notifications WHERE account_id=? AND suppression_key=?",
            (account_id, suppression_key),
        ).fetchone()
        return None if row is None else self._from_row(row)

    def list(self, account_id: str) -> list[Notification]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT * FROM notifications WHERE account_id=? ORDER BY scheduled_for,id", (account_id,)
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def snooze(
        self, *, account_id: str, notification_id: str, until: datetime, expected_version: int
    ) -> Notification:
        current = self.get(account_id, notification_id)
        if current.version != expected_version:
            raise VersionConflict("notification version changed")
        if current.state in (NotificationState.DELIVERED, NotificationState.SUPPRESSED):
            raise ValidationError("delivered/suppressed notification cannot be snoozed")
        if until.tzinfo is None or until.utcoffset() is None:
            raise ValidationError("snooze time must be offset-aware")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE notifications SET state='SNOOZED',snoozed_until=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_iso(until), _iso(now), account_id, notification_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("notification version changed before snooze commit")
        return self.get(account_id, notification_id)

    def due(self, account_id: str, now: datetime | None = None) -> list[Notification]:
        self.canonical._require_account(account_id)
        now = now or self.clock.now()
        result: list[Notification] = []
        for item in self.list(account_id):
            if item.state not in (NotificationState.PENDING, NotificationState.SNOOZED, NotificationState.FAILED):
                continue
            effective = item.snoozed_until or item.scheduled_for
            if item.cooldown_until is not None and item.cooldown_until > effective:
                effective = item.cooldown_until
            if effective <= now:
                result.append(item)
        return result

    def deliver(
        self,
        *,
        account_id: str,
        notification_id: str,
        sender: Callable[[Notification], bool],
    ) -> Notification:
        current = self.get(account_id, notification_id)
        if current.state in (NotificationState.DELIVERED, NotificationState.SUPPRESSED):
            return current
        now = self.clock.now()
        effective_due = current.snoozed_until or current.scheduled_for
        if current.cooldown_until is not None and current.cooldown_until > effective_due:
            effective_due = current.cooldown_until
        if effective_due > now:
            return current

        stale_reason = self._stale_reason(current)
        if stale_reason is not None:
            return self._mark_suppressed(current, stale_reason)
        if current.initial_notification_id is not None:
            initial = self.get(account_id, current.initial_notification_id)
            if initial.state is not NotificationState.DELIVERED:
                return self._mark_suppressed(current, "INITIAL_NOTIFICATION_NOT_DELIVERED")

        try:
            delivered = bool(sender(current))
            error = None if delivered else "CHANNEL_DELIVERY_FAILED"
        except Exception as exc:  # channel boundary: persist one retryable workflow result
            delivered = False
            error = f"CHANNEL_ERROR:{type(exc).__name__}"
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE notifications SET state=?,delivered_at=?,attempt_count=attempt_count+1,last_error=?,"
                "version=version+1,updated_at=? WHERE account_id=? AND id=? AND version=?",
                (
                    NotificationState.DELIVERED.value if delivered else NotificationState.FAILED.value,
                    _iso(now) if delivered else None, error, _iso(now), account_id, notification_id, current.version,
                ),
            )
            if cur.rowcount != 1:
                raise VersionConflict("notification version changed before delivery commit")
        return self.get(account_id, notification_id)

    def _stale_reason(self, item: Notification) -> str | None:
        if self.canonical.get_server_revision(item.account_id) != item.domain_revision:
            return "STALE_DOMAIN_REVISION"
        if item.plan_id is not None:
            latest = SQLitePlanStore(self.canonical).get_latest(item.account_id)
            if latest is None or latest.id != item.plan_id or latest.plan_revision != item.plan_revision:
                return "STALE_PLAN_REVISION"
        return None

    def _mark_suppressed(self, item: Notification, reason: str) -> Notification:
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE notifications SET state='SUPPRESSED',last_error=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (reason, _iso(now), item.account_id, item.id, item.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("notification version changed before suppression commit")
        return self.get(item.account_id, item.id)

    @staticmethod
    def _from_row(row) -> Notification:
        return Notification(
            id=row["id"], account_id=row["account_id"], suppression_key=row["suppression_key"],
            kind=NotificationKind(row["kind"]), entity_ref=row["entity_ref"],
            domain_revision=int(row["domain_revision"]), plan_id=row["plan_id"], plan_revision=row["plan_revision"],
            scheduled_for=_dt(row["scheduled_for"]), state=NotificationState(row["state"]),
            initial_notification_id=row["initial_notification_id"], group_key=row["group_key"],
            cooldown_until=_dt(row["cooldown_until"]), snoozed_until=_dt(row["snoozed_until"]),
            delivered_at=_dt(row["delivered_at"]), attempt_count=int(row["attempt_count"]),
            version=int(row["version"]), last_error=row["last_error"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )
