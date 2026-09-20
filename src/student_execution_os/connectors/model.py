from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol


class ConnectorHealth(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ConnectorSessionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ConnectorEntityState(StrEnum):
    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"


@dataclass(frozen=True)
class ConnectorState:
    id: str
    account_id: str
    source_system_id: str
    provider: str
    scope: str
    checkpoint: str | None
    health: ConnectorHealth
    last_successful_complete_sync_at: datetime | None
    latest_failure_reason: str | None
    connector_version: str
    version: int


@dataclass(frozen=True)
class ConnectorSyncSession:
    id: str
    account_id: str
    connector_id: str
    scope: str
    cursor_before: str | None
    cursor_after: str | None
    is_full_sync: bool
    status: ConnectorSessionStatus
    started_at: datetime
    completed_at: datetime | None
    error_code: str | None
    page_count: int
    record_count: int
    deletion_count: int


@dataclass(frozen=True)
class ConnectorSyncResult:
    session: ConnectorSyncSession
    checkpoint: str | None
    health: ConnectorHealth
    full_resync_performed: bool = False


@dataclass(frozen=True)
class GoogleCalendarPage:
    items: tuple[Mapping[str, Any], ...]
    next_page_token: str | None
    next_sync_token: str | None


class GoogleCalendarTransport(Protocol):
    def list_events(
        self,
        *,
        calendar_id: str,
        sync_token: str | None,
        page_token: str | None,
    ) -> GoogleCalendarPage:
        ...


class GoogleCalendarProviderError(RuntimeError):
    code = "PROVIDER_ERROR"


class GoogleCalendarInvalidSyncToken(GoogleCalendarProviderError):
    code = "INVALID_SYNC_TOKEN"


class GoogleCalendarAuthError(GoogleCalendarProviderError):
    code = "AUTH_UNAVAILABLE"


class GoogleCalendarTransientError(GoogleCalendarProviderError):
    code = "TRANSIENT_PROVIDER_ERROR"


class GoogleCalendarProtocolError(GoogleCalendarProviderError):
    code = "PROVIDER_PROTOCOL_ERROR"
