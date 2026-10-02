"""One agenda of tasks, events and reminders."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from student_execution_os.agent import AuthenticatedPrincipal

from .common import _jsonify

from .base import ApplicationService
from .tasks import TaskService
from .events import EventService
from .notifications import NotificationService


class CommitmentQueries(ApplicationService):
    """One agenda of tasks, events and reminders."""

    def __init__(self, database: str, *, account_id: str, principal: AuthenticatedPrincipal,
                 now: Callable[[], datetime], binding: str, tasks: TaskService, events: EventService,
                 notifications: NotificationService) -> None:
        super().__init__(database, account_id=account_id, principal=principal, now=now, binding=binding)
        self.tasks = tasks
        self.events = events
        self.notifications = notifications

    def commitments(self, place: str | None = None, query: str = "") -> dict[str, Any]:
        """Tasks, events and reminders as one agenda / search result (see web/commitments.py)."""
        from student_execution_os.web.commitments import commitments
        if place not in (None, "", "open", "done", "archive"):
            raise ValueError("place must be open, done or archive")
        tasks, events, reminders = self.tasks.tasks(), self.events.events(), self.notifications.reminders()
        items = commitments(tasks=tasks, events=events, reminders=reminders, now=self._now(),
                            place=place or None, query=query[:200])
        return {"now": _jsonify(self._now()), "items": items}
