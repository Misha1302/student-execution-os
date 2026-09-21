from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.domain.errors import ValidationError
from student_execution_os.notifications import (
    DeliveryReceipt,
    DeliveryState,
    NotificationDeliveryPolicy,
    NotificationKind,
    NotificationState,
    SQLiteNotificationDeliveryOutbox,
    SQLiteNotificationRepository,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def open_repo(path: Path, when: datetime) -> SQLiteCanonicalRepository:
    repo = SQLiteCanonicalRepository(path, clock=FrozenClock(when))
    repo.initialize()
    return repo


class NotificationDeliveryOutboxTests(unittest.TestCase):
    def test_restart_reclaims_expired_lease_with_same_delivery_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "delivery.sqlite3"
            with open_repo(db, BASE) as repo:
                repo.create_account("a")
                notifications = SQLiteNotificationRepository(repo)
                item = notifications.schedule(
                    account_id="a",
                    suppression_key="deadline:t1:warning",
                    kind=NotificationKind.DEADLINE_WARNING,
                    scheduled_for=BASE,
                    domain_revision=repo.get_server_revision("a"),
                    entity_ref="t1",
                )
                outbox = SQLiteNotificationDeliveryOutbox(
                    notifications,
                    policy=NotificationDeliveryPolicy(lease_seconds=30),
                )
                first = outbox.claim(account_id="a", notification_id=item.id, worker_id="worker-1")
                self.assertIsNotNone(first)
                key = first.delivery.delivery_key
                self.assertEqual(first.delivery.state, DeliveryState.LEASED)

            with open_repo(db, BASE + timedelta(seconds=31)) as repo:
                notifications = SQLiteNotificationRepository(repo)
                outbox = SQLiteNotificationDeliveryOutbox(
                    notifications,
                    policy=NotificationDeliveryPolicy(lease_seconds=30),
                )
                second = outbox.claim(account_id="a", notification_id=item.id, worker_id="worker-2")
                self.assertIsNotNone(second)
                self.assertEqual(second.delivery.delivery_key, key)
                self.assertEqual(second.delivery.attempt_count, 2)
                delivered = outbox.complete(
                    account_id="a",
                    notification_id=item.id,
                    worker_id="worker-2",
                    receipt=DeliveryReceipt(delivered=True, provider_message_id="provider-1"),
                )
                self.assertEqual(delivered.state, NotificationState.DELIVERED)
                self.assertEqual(outbox.get("a", item.id).state, DeliveryState.SENT)
                self.assertEqual(outbox.get("a", item.id).provider_message_id, "provider-1")

    def test_retry_backoff_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "retry.sqlite3"
            policy = NotificationDeliveryPolicy(
                lease_seconds=20,
                base_retry_seconds=30,
                max_retry_seconds=60,
                max_attempts=3,
            )
            with open_repo(db, BASE) as repo:
                repo.create_account("a")
                notifications = SQLiteNotificationRepository(repo)
                item = notifications.schedule(
                    account_id="a",
                    suppression_key="source:t1:changed",
                    kind=NotificationKind.SOURCE_CHANGE,
                    scheduled_for=BASE,
                    domain_revision=repo.get_server_revision("a"),
                    entity_ref="t1",
                )
                outbox = SQLiteNotificationDeliveryOutbox(notifications, policy=policy)
                failed = outbox.dispatch(
                    account_id="a",
                    notification_id=item.id,
                    worker_id="worker-1",
                    sender=lambda _item, _key: DeliveryReceipt(delivered=False, error="TRANSIENT"),
                )
                self.assertEqual(failed.state, NotificationState.FAILED)
                self.assertEqual(outbox.get("a", item.id).state, DeliveryState.RETRY_WAIT)

            with open_repo(db, BASE + timedelta(seconds=10)) as repo:
                notifications = SQLiteNotificationRepository(repo)
                outbox = SQLiteNotificationDeliveryOutbox(notifications, policy=policy)
                self.assertIsNone(outbox.claim(account_id="a", notification_id=item.id, worker_id="worker-2"))

            with open_repo(db, BASE + timedelta(seconds=31)) as repo:
                notifications = SQLiteNotificationRepository(repo)
                outbox = SQLiteNotificationDeliveryOutbox(notifications, policy=policy)
                seen: list[str] = []
                delivered = outbox.dispatch(
                    account_id="a",
                    notification_id=item.id,
                    worker_id="worker-2",
                    sender=lambda _item, key: seen.append(key) or DeliveryReceipt(delivered=True),
                )
                self.assertEqual(delivered.state, NotificationState.DELIVERED)
                self.assertEqual(len(seen), 1)
                self.assertEqual(outbox.get("a", item.id).attempt_count, 2)

    def test_stale_revision_is_suppressed_before_channel_call(self):
        with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE)) as repo:
            repo.initialize()
            repo.create_account("a")
            notifications = SQLiteNotificationRepository(repo)
            item = notifications.schedule(
                account_id="a",
                suppression_key="stale",
                kind=NotificationKind.SOURCE_CHANGE,
                scheduled_for=BASE,
                domain_revision=repo.get_server_revision("a"),
            )
            # Advance account a through an ordinary canonical mutation.
            from student_execution_os.domain.model import HardCutoff, Importance, ObligationCategory
            repo.create_task(
                account_id="a",
                obligation_id="t1",
                title="Task",
                category=ObligationCategory.GENERAL,
                importance=Importance.NORMAL,
                estimated_total_effort_minutes=5,
                remaining_effort_minutes=5,
                splittable=False,
                actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.SYSTEM,
            )
            outbox = SQLiteNotificationDeliveryOutbox(notifications)
            calls: list[str] = []
            result = outbox.dispatch(
                account_id="a",
                notification_id=item.id,
                worker_id="worker",
                sender=lambda _item, key: calls.append(key) or True,
            )
            self.assertEqual(result.state, NotificationState.SUPPRESSED)
            self.assertEqual(outbox.get("a", item.id).state, DeliveryState.SUPPRESSED)
            self.assertEqual(calls, [])

    def test_active_lease_blocks_snooze(self):
        with SQLiteCanonicalRepository(":memory:", clock=FrozenClock(BASE)) as repo:
            repo.initialize()
            repo.create_account("a")
            notifications = SQLiteNotificationRepository(repo)
            item = notifications.schedule(
                account_id="a",
                suppression_key="lease",
                kind=NotificationKind.DEADLINE_WARNING,
                scheduled_for=BASE,
                domain_revision=repo.get_server_revision("a"),
            )
            outbox = SQLiteNotificationDeliveryOutbox(notifications)
            lease = outbox.claim(account_id="a", notification_id=item.id, worker_id="worker")
            self.assertIsNotNone(lease)
            current = notifications.get("a", item.id)
            with self.assertRaises(ValidationError):
                notifications.snooze(
                    account_id="a",
                    notification_id=item.id,
                    until=BASE + timedelta(hours=1),
                    expected_version=current.version,
                )


if __name__ == "__main__":
    unittest.main()
