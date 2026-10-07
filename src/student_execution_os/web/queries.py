"""Composition root of the application services used by the web routes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from student_execution_os.agent import AuthenticatedPrincipal

from .services import (
    PlanningQueries,
    ExecutionQueries,
    TaskService,
    ProjectQueries,
    EventService,
    RoutineQueries,
    ConnectorService,
    NotificationService,
    NoteService,
    AccountService,
    AssistantService,
    OperationService,
    CommitmentQueries,
    CheckInQueries,
)
# Re-exported for the host and tests.
from .services.common import TEST_NOTIFICATION_LIMITER, _AccountLimiter

__all__ = ["TEST_NOTIFICATION_LIMITER", "UiService", "_AccountLimiter"]


class UiService:
    """Composition root of the application owners behind the web routes.

    Routes call an explicit owner (``service.tasks.create_task``,
    ``service.planning.today`` …); each owner is a cohesive module in
    ``web/services``. The client never supplies account or principal identity. The host binds those
    when constructing the service (once for a bound server, per request from the
    authenticated session otherwise); every request opens a fresh SQLite adapter
    and applies the bound account at the server boundary.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        account_id: str,
        principal_id: str,
        client_id: str = "web-ui",
        now: Callable[[], datetime] | None = None,
        binding: str = "server-bound",
    ) -> None:
        self.database = str(database)
        self.account_id = account_id
        self.binding = binding
        self.principal = AuthenticatedPrincipal(
            account_id=account_id,
            principal_id=principal_id,
            client_id=client_id,
        )
        # The planner works on whole minutes; a wall clock with seconds would make every
        # plan UNKNOWN (UNSUPPORTED_SUB_MINUTE_TIME).
        self._now = now or (lambda: datetime.now(timezone.utc).replace(second=0, microsecond=0))
        context = {"account_id": account_id, "principal": self.principal, "now": self._now, "binding": binding}
        self.planning = PlanningQueries(self.database, **context)
        self.execution = ExecutionQueries(self.database, **context)
        self.tasks = TaskService(self.database, **context)
        self.projects = ProjectQueries(self.database, **context)
        self.events = EventService(self.database, **context)
        self.routines = RoutineQueries(self.database, **context)
        self.connectors = ConnectorService(self.database, **context)
        self.notifications = NotificationService(self.database, **context)
        self.notes = NoteService(self.database, **context)
        self.account = AccountService(self.database, **context)
        self.assistant = AssistantService(self.database, **context)
        self.operations = OperationService(self.database, **context)
        self.checkins = CheckInQueries(self.database, **context)
        self.commitments = CommitmentQueries(self.database, **context, tasks=self.tasks, events=self.events,
                                             notifications=self.notifications)
