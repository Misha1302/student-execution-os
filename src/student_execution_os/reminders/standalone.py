"""Standalone reminders ("напомни купить хлеб завтра в 18", "разбуди меня в 7").

A reminder is its own small entity, not a Task (nothing to plan or estimate) and not
an Event (no interval). Its lifecycle, as the user sees it:

    SCHEDULED ──(its moment)──▶ FIRED ──(Готово / «Я встал» + awake check)──▶ DONE
        ▲            │                 │
        └─ snooze ◀──┴─────────────────┘            cancel ▶ CANCELLED ─ reopen ▶ SCHEDULED

Delivery is how it gets attention: PUSH (a notification), ALARM (the Android phone
rings a real alarm, scheduled on the device so it works offline and in Doze) or
PUSH_AND_ALARM. A wake alarm (``wake_check``) is only DONE once the user answered the
awake check after «Я встал»; the device asks and re-rings on its own.

Reminder rows are workflow state owned by the account; mutations come only through
the idempotent sync commands (sync/commands.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

DELIVERIES = ("PUSH", "ALARM", "PUSH_AND_ALARM")
OPEN = ("SCHEDULED", "FIRED")
# A reminder whose moment passed while nothing could send it (server down) still
# goes out this long afterwards; older ones are marked FIRED without a message.
FIRE_GRACE = timedelta(hours=12)
# After «Я встал» the device asks again this much later, and rings again this long
# after an unanswered check (see the Android WakeAlarm package).
AWAKE_CHECK_AFTER = timedelta(minutes=25)
AWAKE_REWAKE_AFTER = timedelta(minutes=10)

_COLUMNS = ("id,title,note,remind_at,delivery,wake_check,raise_volume,obligation_id,status,fired_at,acknowledged_at,"
            "awake_confirmed_at,completed_at,snooze_count,actor_category,created_at,updated_at,version")


def has_alarm(delivery: str) -> bool:
    return delivery in ("ALARM", "PUSH_AND_ALARM")


def reminder_payload(row) -> dict[str, Any]:
    item = {key: row[key] for key in _COLUMNS.split(",")}
    item["kind"] = "REMINDER"
    item["wake_check"] = bool(item["wake_check"])
    item["raise_volume"] = bool(item["raise_volume"])
    item.pop("actor_category")
    return item


@dataclass(frozen=True)
class Change:
    reminder: dict[str, Any]
    changed: bool
    code: str | None = None


class SQLiteReminderRepository:
    def __init__(self, repo: SQLiteCanonicalRepository) -> None:
        self.repo = repo
        self.connection = repo.connection

    # ---- reads ----------------------------------------------------------------------

    def _row(self, account_id: str, reminder_id: str):
        row = self.connection.execute(f"SELECT {_COLUMNS} FROM reminders WHERE account_id=? AND id=?",
                                      (account_id, reminder_id)).fetchone()
        if row is None:
            raise EntityNotFound("reminder not found")
        return row

    def get(self, account_id: str, reminder_id: str) -> dict[str, Any]:
        return reminder_payload(self._row(account_id, reminder_id))

    def exists(self, account_id: str, reminder_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM reminders WHERE account_id=? AND id=?",
                                       (account_id, reminder_id)).fetchone() is not None

    def is_deleted(self, account_id: str, reminder_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM deleted_reminders WHERE account_id=? AND reminder_id=?",
                                       (account_id, reminder_id)).fetchone() is not None

    def list(self, account_id: str, *, since: datetime | None = None) -> list[dict[str, Any]]:
        """Open reminders, plus closed ones touched since ``since`` (history for the lists)."""
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE account_id=? AND (status IN ('SCHEDULED','FIRED') OR updated_at>=?) "
            "ORDER BY remind_at", (account_id, _iso(since) if since else ""),
        ).fetchall()
        return [reminder_payload(row) for row in rows]

    def upcoming_alarms(self, account_id: str, now: datetime) -> list[dict[str, Any]]:
        """What an Android phone should have scheduled: open alarm reminders not yet over."""
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE account_id=? AND delivery IN ('ALARM','PUSH_AND_ALARM') "
            "AND status IN ('SCHEDULED','FIRED') AND remind_at>=?",
            (account_id, _iso(now - FIRE_GRACE)),
        ).fetchall()
        return [reminder_payload(row) for row in rows]

    def due(self, account_id: str, now: datetime) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM reminders WHERE account_id=? AND status='SCHEDULED' AND remind_at<=? ORDER BY remind_at",
            (account_id, _iso(now)),
        ).fetchall()
        return [reminder_payload(row) for row in rows]

    def next_due_at(self, account_id: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT min(remind_at) FROM reminders WHERE account_id=? AND status='SCHEDULED'", (account_id,)
        ).fetchone()
        return _dt(row[0]) if row and row[0] else None

    # ---- writes (run inside the sync command transaction) -----------------------------

    def _write(self, account_id: str, reminder_id: str, now: datetime, **fields: Any) -> dict[str, Any]:
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.repo._tx() as conn:
            conn.execute(f"UPDATE reminders SET {assignments},updated_at=?,version=version+1 WHERE account_id=? AND id=?",
                         (*fields.values(), _iso(now), account_id, reminder_id))
        return self.get(account_id, reminder_id)

    def create(self, account_id: str, reminder_id: str, *, title: str, remind_at: datetime, delivery: str,
               note: str | None, wake_check: bool, raise_volume: bool, obligation_id: str | None,
               actor: str, now: datetime) -> dict[str, Any]:
        if delivery not in DELIVERIES:
            raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
        if (wake_check or raise_volume) and not has_alarm(delivery):
            raise ValidationError("a wake check and the alarm volume need an alarm delivery")
        if obligation_id and self.connection.execute(
                "SELECT 1 FROM obligations WHERE account_id=? AND id=?", (account_id, obligation_id)).fetchone() is None:
            raise ValidationError("the reminder refers to an unknown task or event")
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO reminders(id,account_id,title,note,remind_at,delivery,wake_check,raise_volume,obligation_id,"
                "status,actor_category,created_at,updated_at,version) VALUES (?,?,?,?,?,?,?,?,?,'SCHEDULED',?,?,?,1)",
                (reminder_id, account_id, title, note, _iso(remind_at), delivery, int(wake_check), int(raise_volume),
                 obligation_id or None, actor, _iso(now), _iso(now)),
            )
        return self.get(account_id, reminder_id)

    def update(self, account_id: str, reminder_id: str, fields: dict[str, Any], now: datetime) -> dict[str, Any]:
        current = self.get(account_id, reminder_id)
        delivery = fields.get("delivery", current["delivery"])
        wake = fields.get("wake_check", current["wake_check"])
        loud = fields.get("raise_volume", current["raise_volume"])
        if not has_alarm(delivery):
            wake, loud = False, False  # switching to a plain notification drops alarm options
        values: dict[str, Any] = {key: fields[key] for key in ("title", "note") if key in fields}
        values.update(delivery=delivery, wake_check=int(wake), raise_volume=int(loud))
        if "remind_at" in fields:
            # A new moment is a new reminder episode: it will fire (and ring) again.
            values.update(remind_at=_iso(fields["remind_at"]), status="SCHEDULED", fired_at=None,
                          acknowledged_at=None, awake_confirmed_at=None, completed_at=None)
        return self._write(account_id, reminder_id, now, **values)

    def snooze(self, account_id: str, reminder_id: str, until: datetime, now: datetime) -> dict[str, Any]:
        current = self.get(account_id, reminder_id)
        return self._write(account_id, reminder_id, now, remind_at=_iso(until), status="SCHEDULED", fired_at=None,
                           acknowledged_at=None, awake_confirmed_at=None, snooze_count=current["snooze_count"] + 1)

    def mark_fired(self, account_id: str, reminder_id: str, now: datetime) -> None:
        with self.repo._tx() as conn:
            conn.execute("UPDATE reminders SET status='FIRED',fired_at=?,updated_at=? WHERE account_id=? AND id=? "
                         "AND status='SCHEDULED'", (_iso(now), _iso(now), account_id, reminder_id))

    def done(self, account_id: str, reminder_id: str, now: datetime) -> dict[str, Any]:
        return self._write(account_id, reminder_id, now, status="DONE", completed_at=_iso(now))

    def acknowledge(self, account_id: str, reminder_id: str, stage: str, now: datetime) -> dict[str, Any]:
        """«Я встал» (UP) and the answer to the awake check (AWAKE)."""
        current = self.get(account_id, reminder_id)
        if stage == "UP":
            fields: dict[str, Any] = {"acknowledged_at": current["acknowledged_at"] or _iso(now),
                                      "status": "FIRED", "fired_at": current["fired_at"] or _iso(now)}
            if not current["wake_check"]:
                fields.update(status="DONE", completed_at=_iso(now))
            return self._write(account_id, reminder_id, now, **fields)
        if stage == "AWAKE":
            return self._write(account_id, reminder_id, now, awake_confirmed_at=_iso(now),
                               acknowledged_at=current["acknowledged_at"] or _iso(now),
                               status="DONE", completed_at=_iso(now))
        raise ValidationError("stage must be UP or AWAKE")

    def cancel(self, account_id: str, reminder_id: str, now: datetime) -> dict[str, Any]:
        return self._write(account_id, reminder_id, now, status="CANCELLED")

    def reopen(self, account_id: str, reminder_id: str, now: datetime) -> dict[str, Any]:
        current = self.get(account_id, reminder_id)
        fields: dict[str, Any] = {"status": "SCHEDULED", "completed_at": None, "fired_at": None,
                                  "acknowledged_at": None, "awake_confirmed_at": None}
        if _dt(current["remind_at"]) <= now:
            fields["remind_at"] = _iso(now + timedelta(minutes=10))  # back on, a little later
        return self._write(account_id, reminder_id, now, **fields)

    def delete(self, account_id: str, reminder_id: str, now: datetime) -> None:
        self._row(account_id, reminder_id)
        with self.repo._tx() as conn:
            conn.execute(
                "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                "last_error='DELETED' WHERE account_id=? AND reminder_id=? AND delivery_state IN ('PENDING','LEASED')",
                (account_id, reminder_id),
            )
            conn.execute("DELETE FROM reminders WHERE account_id=? AND id=?", (account_id, reminder_id))
            conn.execute("INSERT OR REPLACE INTO deleted_reminders(account_id,reminder_id,deleted_at) VALUES (?,?,?)",
                         (account_id, reminder_id, _iso(now)))

    def cancel_pending_messages(self, account_id: str, reminder_id: str, reason: str) -> None:
        with self.repo._tx() as conn:
            conn.execute(
                "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                "last_error=? WHERE account_id=? AND reminder_id=? AND delivery_state='PENDING'",
                (reason, account_id, reminder_id),
            )
