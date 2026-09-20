"""Source connector boundary for Pass 5."""

from .google_calendar import GoogleCalendarConnector, GoogleCalendarHTTPTransport
from .model import (
    ConnectorEntityState,
    ConnectorHealth,
    ConnectorSessionStatus,
    ConnectorState,
    ConnectorSyncResult,
    ConnectorSyncSession,
    GoogleCalendarAuthError,
    GoogleCalendarInvalidSyncToken,
    GoogleCalendarPage,
    GoogleCalendarProtocolError,
    GoogleCalendarProviderError,
    GoogleCalendarTransientError,
    GoogleCalendarTransport,
)
from .repository import SQLiteConnectorRepository

__all__ = [
    "ConnectorEntityState",
    "ConnectorHealth",
    "ConnectorSessionStatus",
    "ConnectorState",
    "ConnectorSyncResult",
    "ConnectorSyncSession",
    "GoogleCalendarAuthError",
    "GoogleCalendarConnector",
    "GoogleCalendarHTTPTransport",
    "GoogleCalendarInvalidSyncToken",
    "GoogleCalendarPage",
    "GoogleCalendarProtocolError",
    "GoogleCalendarProviderError",
    "GoogleCalendarTransientError",
    "GoogleCalendarTransport",
    "SQLiteConnectorRepository",
]
