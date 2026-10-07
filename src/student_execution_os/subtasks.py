"""Checklists inside a Task (schema v33, ADR 0036).

A subtask is a step of one Task — never a hidden top-level Task. The planner keeps
working with the Task's remaining effort only; a subtask's effort is a breakdown the
user may give, shown next to the Task's estimate, never added to the plan, so nothing
is counted twice. Completing a subtask is a fact about the step; it does not complete
the Task or change its remaining effort by itself (the user does that, as before).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

_COLUMNS = "id,task_id,title,position,effort_minutes,done_at,version,created_at,updated_at"
MAX_ITEMS = 200


def _payload(row) -> dict[str, Any]:
    item = {key: row[key] for key in _COLUMNS.split(",")}
    item["kind"] = "SUBTASK"
    item["done"] = row["done_at"] is not None
    return item


def checklist_summary(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Progress of a checklist: done share by effort when every step has one, else by count."""
    if not items:
        return None
    done = [item for item in items if item["done"]]
    efforts = [item["effort_minutes"] for item in items]
    by_effort = all(effort is not None for effort in efforts)
    if by_effort:
        total = sum(efforts)
        percent = round(100 * sum(item["effort_minutes"] for item in done) / total) if total else 0
    else:
        percent = round(100 * len(done) / len(items))
    return {"total": len(items), "done": len(done), "percent": percent, "basis": "EFFORT" if by_effort else "COUNT",
            "effort_minutes": sum(efforts) if by_effort else None,
            "effort_left_minutes": sum(item["effort_minutes"] for item in items if not item["done"]) if by_effort else None}


class SQLiteSubtaskRepository:
    def __init__(self, repo: SQLiteCanonicalRepository) -> None:
        self.repo = repo
        self.connection = repo.connection

    def get(self, account_id: str, subtask_id: str) -> dict[str, Any]:
        row = self.connection.execute(f"SELECT {_COLUMNS} FROM task_subtasks WHERE account_id=? AND id=?",
                                      (account_id, subtask_id)).fetchone()
        if row is None:
            raise EntityNotFound("subtask not found")
        return _payload(row)

    def owner_of(self, subtask_id: str) -> str | None:
        row = self.connection.execute("SELECT account_id FROM task_subtasks WHERE id=?", (subtask_id,)).fetchone()
        return None if row is None else row["account_id"]

    def for_task(self, account_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM task_subtasks WHERE account_id=? AND task_id=? ORDER BY position,id",
            (account_id, task_id)).fetchall()
        return [_payload(row) for row in rows]

    def summaries(self, account_id: str) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM task_subtasks WHERE account_id=? ORDER BY task_id,position,id", (account_id,)).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["task_id"], []).append(_payload(row))
        return {task_id: checklist_summary(items) for task_id, items in grouped.items()}

    def _record(self, conn, account_id: str, subtask_id: str, action: str, actor: ActorCategory, task_id: str) -> None:
        self.repo._record_change(conn, account_id=account_id, entity_type="SUBTASK", entity_id=subtask_id,
                                 action=action, actor=actor, payload={"task_id": task_id})

    def create(self, *, account_id: str, subtask_id: str, task_id: str, title: str, effort_minutes: int | None,
               position: float | None, actor: ActorCategory, now: datetime) -> dict[str, Any]:
        task = self.connection.execute(
            "SELECT kind FROM obligations WHERE account_id=? AND id=?", (account_id, task_id)).fetchone()
        if task is None or task["kind"] != "TASK":
            raise EntityNotFound("task not found")
        count = self.connection.execute("SELECT count(*) FROM task_subtasks WHERE account_id=? AND task_id=?",
                                        (account_id, task_id)).fetchone()[0]
        if count >= MAX_ITEMS:
            raise ValidationError(f"a checklist holds at most {MAX_ITEMS} steps")
        if position is None:
            last = self.connection.execute("SELECT max(position) FROM task_subtasks WHERE account_id=? AND task_id=?",
                                           (account_id, task_id)).fetchone()[0]
            position = 1.0 if last is None else float(last) + 1.0
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO task_subtasks(id,account_id,task_id,title,position,effort_minutes,version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?)",
                (subtask_id, account_id, task_id, title, float(position), effort_minutes, _iso(now), _iso(now)))
            self._record(conn, account_id, subtask_id, "CREATE_SUBTASK", actor, task_id)
        return self.get(account_id, subtask_id)

    def update(self, account_id: str, subtask_id: str, fields: dict[str, Any], *, action: str, actor: ActorCategory,
               now: datetime) -> dict[str, Any]:
        current = self.get(account_id, subtask_id)
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.repo._tx() as conn:
            conn.execute(f"UPDATE task_subtasks SET {assignments},version=version+1,updated_at=? WHERE account_id=? AND id=?",
                         (*fields.values(), _iso(now), account_id, subtask_id))
            self._record(conn, account_id, subtask_id, action, actor, current["task_id"])
        return self.get(account_id, subtask_id)

    def delete(self, account_id: str, subtask_id: str, *, actor: ActorCategory, now: datetime) -> None:
        current = self.get(account_id, subtask_id)
        with self.repo._tx() as conn:
            conn.execute("DELETE FROM task_subtasks WHERE account_id=? AND id=?", (account_id, subtask_id))
            conn.execute("INSERT OR REPLACE INTO deleted_entities(account_id,entity_kind,entity_id,deleted_at) "
                         "VALUES (?,'SUBTASK',?,?)", (account_id, subtask_id, _iso(now)))
            self._record(conn, account_id, subtask_id, "DELETE_SUBTASK", actor, current["task_id"])
