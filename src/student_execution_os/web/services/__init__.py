"""Application owners behind the web routes (composed by ``web.queries.UiService``)."""
from .planning import PlanningQueries
from .execution import ExecutionQueries
from .tasks import TaskService
from .projects import ProjectQueries
from .events import EventService
from .routines import RoutineQueries
from .connectors import ConnectorService
from .notifications import NotificationService
from .notes import NoteService
from .account import AccountService
from .assistant import AssistantService
from .operations import OperationService
from .commitments import CommitmentQueries

__all__ = ["PlanningQueries", "ExecutionQueries", "TaskService", "ProjectQueries", "EventService", "RoutineQueries", "ConnectorService", "NotificationService", "NoteService", "AccountService", "AssistantService", "OperationService", "CommitmentQueries"]
