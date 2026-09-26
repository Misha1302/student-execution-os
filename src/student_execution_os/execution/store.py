from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory, LifecycleStatus
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso


OPEN_TASK = {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}
NON_TERMINAL = {"ACTIVE", "PAUSED"}


def _dt(value: str | None) -> datetime | None:
    return None if not value else datetime.fromisoformat(value)


class SQLiteExecutionStore:
    """Canonical actual-work history.

    Plan blocks are never mutated here. A session may reference the plan/block that
    suggested the work, but those references are advisory provenance only.
    """

    def __init__(self, repo: SQLiteCanonicalRepository) -> None:
        self.repo = repo

    def _row(self, account_id: str, session_id: str):
        row = self.repo.connection.execute(
            "SELECT * FROM execution_sessions WHERE account_id=? AND id=?",
            (account_id, session_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("execution session not found")
        return row

    def _seconds(self, account_id: str, session_id: str, now: datetime) -> tuple[int, str | None]:
        rows = self.repo.connection.execute(
            "SELECT started_at,ended_at FROM execution_segments "
            "WHERE account_id=? AND session_id=? ORDER BY started_at,id",
            (account_id, session_id),
        ).fetchall()
        total = 0
        current = None
        for row in rows:
            start = _dt(row["started_at"])
            end = _dt(row["ended_at"])
            if start is None:
                continue
            if end is None:
                current = row["started_at"]
                end = now
            total += max(0, int((end - start).total_seconds()))
        return total, current

    def payload(self, account_id: str, session_id: str, now: datetime) -> dict[str, Any]:
        row = self._row(account_id, session_id)
        seconds, current = self._seconds(account_id, session_id, now)
        task = self.repo.connection.execute(
            "SELECT title FROM obligations WHERE account_id=? AND id=?",
            (account_id, row["task_id"]),
        ).fetchone()
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "task_title": None if task is None else task["title"],
            "state": row["state"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "planning_snapshot_id": row["planning_snapshot_id"],
            "source_plan_block_id": row["source_plan_block_id"],
            "remaining_effort_at_start": row["remaining_effort_at_start"],
            "estimated_total_effort_at_start": row["estimated_total_effort_at_start"],
            "actual_work_seconds": seconds,
            "actual_work_minutes": seconds // 60,
            "current_segment_started_at": current,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": int(row["version"]),
        }

    def active(self, account_id: str, now: datetime) -> dict[str, Any] | None:
        row = self.repo.connection.execute(
            "SELECT id FROM execution_sessions WHERE account_id=? "
            "AND state IN ('ACTIVE','PAUSED') ORDER BY started_at LIMIT 1",
            (account_id,),
        ).fetchone()
        return None if row is None else self.payload(account_id, row["id"], now)

    def list(
        self,
        account_id: str,
        now: datetime,
        *,
        task_id: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT id FROM execution_sessions WHERE account_id=?"
        args: list[Any] = [account_id]
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if since:
            sql += " AND started_at>=?"
            args.append(_iso(since))
        sql += " ORDER BY started_at DESC,id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = self.repo.connection.execute(sql, tuple(args)).fetchall()
        return [self.payload(account_id, row["id"], now) for row in rows]

    def _audit(self, account_id: str, session_id: str, action: str, actor: ActorCategory) -> None:
        self.repo._record_change(
            self.repo.connection,
            account_id=account_id,
            entity_type="EXECUTION_SESSION",
            entity_id=session_id,
            action=action,
            actor=actor,
        )

    def start(
        self,
        account_id: str,
        session_id: str,
        task_id: str,
        now: datetime,
        actor: ActorCategory,
        *,
        planning_snapshot_id: str | None = None,
        source_plan_block_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        existing = self.repo.connection.execute(
            "SELECT state,task_id FROM execution_sessions WHERE account_id=? AND id=?",
            (account_id, session_id),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] == task_id and existing["state"] in NON_TERMINAL:
                return self.payload(account_id, session_id, now), False
            raise ValidationError("execution session id is already in use")
        active = self.active(account_id, now)
        if active is not None:
            raise VersionConflict(f"another execution session is active: {active['id']}")
        task = self.repo.get_task(account_id, task_id)
        if task.obligation.lifecycle_status not in OPEN_TASK:
            raise VersionConflict("task is closed; reopen it before starting work")
        self.repo.connection.execute(
            "INSERT INTO execution_sessions("
            "id,account_id,task_id,state,started_at,planning_snapshot_id,source_plan_block_id,"
            "remaining_effort_at_start,estimated_total_effort_at_start,created_at,updated_at,version"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                session_id, account_id, task_id, "ACTIVE", _iso(now),
                planning_snapshot_id, source_plan_block_id,
                task.remaining_effort_minutes, task.estimated_total_effort_minutes,
                _iso(now), _iso(now),
            ),
        )
        self.repo.connection.execute(
            "INSERT INTO execution_segments(id,account_id,session_id,started_at,created_at) VALUES (?,?,?,?,?)",
            (f"segment-{uuid4()}", account_id, session_id, _iso(now), _iso(now)),
        )
        self._audit(account_id, session_id, "EXECUTION_STARTED", actor)
        return self.payload(account_id, session_id, now), True

    def pause(self, account_id: str, session_id: str, now: datetime, actor: ActorCategory) -> tuple[dict[str, Any], bool]:
        row = self._row(account_id, session_id)
        if row["state"] == "PAUSED":
            return self.payload(account_id, session_id, now), False
        if row["state"] != "ACTIVE":
            raise VersionConflict("only an active execution session can be paused")
        self.repo.connection.execute(
            "UPDATE execution_segments SET ended_at=? WHERE account_id=? AND session_id=? AND ended_at IS NULL",
            (_iso(now), account_id, session_id),
        )
        self.repo.connection.execute(
            "UPDATE execution_sessions SET state='PAUSED',updated_at=?,version=version+1 "
            "WHERE account_id=? AND id=?",
            (_iso(now), account_id, session_id),
        )
        self._audit(account_id, session_id, "EXECUTION_PAUSED", actor)
        return self.payload(account_id, session_id, now), True

    def resume(self, account_id: str, session_id: str, now: datetime, actor: ActorCategory) -> tuple[dict[str, Any], bool]:
        row = self._row(account_id, session_id)
        if row["state"] == "ACTIVE":
            return self.payload(account_id, session_id, now), False
        if row["state"] != "PAUSED":
            raise VersionConflict("only a paused execution session can be resumed")
        self.repo.connection.execute(
            "INSERT INTO execution_segments(id,account_id,session_id,started_at,created_at) VALUES (?,?,?,?,?)",
            (f"segment-{uuid4()}", account_id, session_id, _iso(now), _iso(now)),
        )
        self.repo.connection.execute(
            "UPDATE execution_sessions SET state='ACTIVE',updated_at=?,version=version+1 "
            "WHERE account_id=? AND id=?",
            (_iso(now), account_id, session_id),
        )
        self._audit(account_id, session_id, "EXECUTION_RESUMED", actor)
        return self.payload(account_id, session_id, now), True

    def finish(self, account_id: str, session_id: str, now: datetime, actor: ActorCategory) -> tuple[dict[str, Any], bool]:
        row = self._row(account_id, session_id)
        if row["state"] == "FINISHED":
            return self.payload(account_id, session_id, now), False
        if row["state"] == "CANCELLED":
            raise VersionConflict("cancelled execution session cannot be finished")
        if row["state"] == "ACTIVE":
            self.repo.connection.execute(
                "UPDATE execution_segments SET ended_at=? WHERE account_id=? AND session_id=? AND ended_at IS NULL",
                (_iso(now), account_id, session_id),
            )
        self.repo.connection.execute(
            "UPDATE execution_sessions SET state='FINISHED',finished_at=?,updated_at=?,version=version+1 "
            "WHERE account_id=? AND id=?",
            (_iso(now), _iso(now), account_id, session_id),
        )
        self._audit(account_id, session_id, "EXECUTION_FINISHED", actor)
        return self.payload(account_id, session_id, now), True

    def cancel(self, account_id: str, session_id: str, now: datetime, actor: ActorCategory) -> tuple[dict[str, Any], bool]:
        row = self._row(account_id, session_id)
        if row["state"] == "CANCELLED":
            return self.payload(account_id, session_id, now), False
        if row["state"] == "FINISHED":
            raise VersionConflict("finished execution session cannot be cancelled")
        if row["state"] == "ACTIVE":
            self.repo.connection.execute(
                "UPDATE execution_segments SET ended_at=? WHERE account_id=? AND session_id=? AND ended_at IS NULL",
                (_iso(now), account_id, session_id),
            )
        self.repo.connection.execute(
            "UPDATE execution_sessions SET state='CANCELLED',finished_at=?,updated_at=?,version=version+1 "
            "WHERE account_id=? AND id=?",
            (_iso(now), _iso(now), account_id, session_id),
        )
        self._audit(account_id, session_id, "EXECUTION_CANCELLED", actor)
        return self.payload(account_id, session_id, now), True

    def finish_active_for_task(
        self, account_id: str, task_id: str, now: datetime, actor: ActorCategory
    ) -> dict[str, Any] | None:
        active = self.active(account_id, now)
        if active is None or active["task_id"] != task_id:
            return None
        return self.finish(account_id, active["id"], now, actor)[0]
