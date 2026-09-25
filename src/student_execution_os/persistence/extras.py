"""Per-item settings that sit beside the canonical Task/Event rows (schema v15).

* counted progress of a task ("3 of 10 problems");
* "remind me N minutes before" of a fixed-time event.

Neither is a planning input: the planner keeps working in minutes, and the event
reminder moment itself is stored as a reminder request (``reminder_states.remind_at``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import ValidationError

from .sqlite import SQLiteCanonicalRepository, _iso

MAX_EVENT_LEAD = 1440


def progress_counts(repo: SQLiteCanonicalRepository, account_id: str) -> dict[str, dict[str, Any]]:
    rows = repo.connection.execute(
        "SELECT task_id,total,done,unit FROM task_progress_counts WHERE account_id=?", (account_id,)
    ).fetchall()
    return {row["task_id"]: {"total": int(row["total"]), "done": int(row["done"]), "unit": row["unit"]} for row in rows}


def progress_count(repo: SQLiteCanonicalRepository, account_id: str, task_id: str) -> dict[str, Any] | None:
    return progress_counts_for(repo, account_id, [task_id]).get(task_id)


def progress_counts_for(repo: SQLiteCanonicalRepository, account_id: str, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not task_ids:
        return {}
    marks = ",".join("?" for _ in task_ids)
    rows = repo.connection.execute(
        f"SELECT task_id,total,done,unit FROM task_progress_counts WHERE account_id=? AND task_id IN ({marks})",
        (account_id, *task_ids),
    ).fetchall()
    return {row["task_id"]: {"total": int(row["total"]), "done": int(row["done"]), "unit": row["unit"]} for row in rows}


def clean_unit(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if len(text) > 40:
        raise ValidationError("progress unit is longer than 40 characters")
    return text or None


def set_progress_count(repo: SQLiteCanonicalRepository, account_id: str, task_id: str, *,
                       total: int | None, done: int, unit: str | None, now: datetime) -> dict[str, Any] | None:
    """Replace the counted progress of a task; ``total=None`` removes it."""
    with repo._tx() as conn:
        if total is None:
            conn.execute("DELETE FROM task_progress_counts WHERE account_id=? AND task_id=?", (account_id, task_id))
            return None
        if not 1 <= total <= 100_000:
            raise ValidationError("progress total must be between 1 and 100000")
        done = max(0, min(int(done), total))
        conn.execute(
            "INSERT INTO task_progress_counts(account_id,task_id,total,done,unit,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(account_id,task_id) DO UPDATE SET total=excluded.total,done=excluded.done,"
            "unit=excluded.unit,updated_at=excluded.updated_at",
            (account_id, task_id, total, done, unit, _iso(now)),
        )
    return {"total": total, "done": done, "unit": unit}


def event_leads(repo: SQLiteCanonicalRepository, account_id: str) -> dict[str, int]:
    rows = repo.connection.execute(
        "SELECT event_id,lead_minutes FROM event_reminders WHERE account_id=?", (account_id,)
    ).fetchall()
    return {row["event_id"]: int(row["lead_minutes"]) for row in rows}


def event_lead(repo: SQLiteCanonicalRepository, account_id: str, event_id: str) -> int | None:
    row = repo.connection.execute(
        "SELECT lead_minutes FROM event_reminders WHERE account_id=? AND event_id=?", (account_id, event_id)
    ).fetchone()
    return None if row is None else int(row["lead_minutes"])


def parse_lead(value: Any) -> int | None:
    """``None``/"" means "no reminder"; otherwise whole minutes before the start."""
    if value is None or value == "":
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("remind_before_minutes must be a whole number of minutes") from exc
    if not 0 <= minutes <= MAX_EVENT_LEAD:
        raise ValidationError("remind_before_minutes is out of range")
    return minutes


def set_event_lead(repo: SQLiteCanonicalRepository, account_id: str, event_id: str, lead: int | None) -> None:
    with repo._tx() as conn:
        if lead is None:
            conn.execute("DELETE FROM event_reminders WHERE account_id=? AND event_id=?", (account_id, event_id))
        else:
            conn.execute(
                "INSERT INTO event_reminders(account_id,event_id,lead_minutes) VALUES (?,?,?) "
                "ON CONFLICT(account_id,event_id) DO UPDATE SET lead_minutes=excluded.lead_minutes",
                (account_id, event_id, lead),
            )
