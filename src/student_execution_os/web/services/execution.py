"""Actual work sessions."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.execution import SQLiteExecutionStore

from .common import _jsonify

from .base import ApplicationService


class ExecutionQueries(ApplicationService):
    """Actual work sessions."""

    def execution_active(self) -> dict[str, Any]:
        with self._repo() as repo:
            return {"now": _jsonify(self._now()), "session": SQLiteExecutionStore(repo).active(self.account_id, self._now())}

    def execution_sessions(self, task_id: str | None = None, days: int = 90) -> dict[str, Any]:
        days = max(1, min(int(days), 3650))
        with self._repo() as repo:
            sessions = SQLiteExecutionStore(repo).list(
                self.account_id, self._now(), task_id=task_id,
                since=self._now() - timedelta(days=days), limit=500,
            )
            return {"now": _jsonify(self._now()), "sessions": sessions}

    def execution_session(self, session_id: str) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteExecutionStore(repo).payload(self.account_id, session_id, self._now())
