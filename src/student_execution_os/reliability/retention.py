"""Retention of short-lived personal data.

Some rows exist only to make an interaction safe, not as a record the user keeps:

* ``assistant_batches`` hold the (e-mail-redacted) text a user typed or dictated and
  the proposal made from it. A preview can be applied for 30 minutes; after that the
  row is deleted, not merely refused.
* ``assistant_apply_records`` make an Apply exactly-once; they keep result ids only.
* ``client_operations`` make offline sync exactly-once and repeat task titles in
  their stored results.
* terminal ``reminder_messages`` form the reminder inbox (the app shows 30 days).

``purge_expired`` deletes each class after its window. The reminder worker runs it
periodically; nothing here touches canonical tasks, events or evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

ASSISTANT_APPLY_RECORD_RETENTION = timedelta(days=30)
CLIENT_OPERATION_RETENTION = timedelta(days=90)
REMINDER_MESSAGE_RETENTION = timedelta(days=90)

RETENTION_POLICY = {
    "assistant_input": "deleted when the 30-minute preview expires",
    "assistant_apply_records_days": ASSISTANT_APPLY_RECORD_RETENTION.days,
    "offline_operation_log_days": CLIENT_OPERATION_RETENTION.days,
    "reminder_inbox_days": REMINDER_MESSAGE_RETENTION.days,
}


def purge_expired_in(repo: SQLiteCanonicalRepository, now: datetime) -> dict[str, int]:
    with repo._tx() as conn:
        return {
            "assistant_batches": conn.execute(
                "DELETE FROM assistant_batches WHERE expires_at<=?", (_iso(now),)).rowcount,
            "assistant_apply_records": conn.execute(
                "DELETE FROM assistant_apply_records WHERE created_at<?",
                (_iso(now - ASSISTANT_APPLY_RECORD_RETENTION),)).rowcount,
            "client_operations": conn.execute(
                "DELETE FROM client_operations WHERE created_at<?", (_iso(now - CLIENT_OPERATION_RETENTION),)).rowcount,
            "reminder_messages": conn.execute(
                "DELETE FROM reminder_messages WHERE created_at<? AND delivery_state IN ('SENT','NO_DEVICE','CANCELLED','DEAD')",
                (_iso(now - REMINDER_MESSAGE_RETENTION),)).rowcount,
        }


def purge_expired(database: str, now: datetime) -> dict[str, int]:
    with SQLiteCanonicalRepository(database, clock=FrozenClock(now)) as repo:
        repo.initialize()
        return purge_expired_in(repo, now)
