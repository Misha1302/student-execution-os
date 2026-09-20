from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.reconciliation import (
    ExtractionCertainty,
    ObservationValueType,
    SQLiteReconciliationRepository,
)

from .model import (
    ConnectorEntityState,
    ConnectorSyncResult,
    GoogleCalendarAuthError,
    GoogleCalendarInvalidSyncToken,
    GoogleCalendarPage,
    GoogleCalendarProtocolError,
    GoogleCalendarProviderError,
    GoogleCalendarTransientError,
    GoogleCalendarTransport,
)
from .repository import SQLiteConnectorRepository


_PROVIDER = "GOOGLE_CALENDAR"
_CONNECTOR_VERSION = "1"
_EXTRACTOR_ID = "connector:google-calendar:v1"


def _stable_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "|".join((f"student-execution-os:{kind}", *parts))))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_provider_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GoogleCalendarProtocolError(
            "Google Calendar event.updated must be offset-aware"
        )
    return parsed.astimezone(timezone.utc)


class GoogleCalendarHTTPTransport:
    """Minimal real Google Calendar Events REST transport.

    Access tokens are supplied through a callback and never persisted here.
    """

    def __init__(
        self,
        access_token_provider: Callable[[], str],
        *,
        timeout_seconds: float = 15.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._access_token_provider = access_token_provider
        self._timeout_seconds = timeout_seconds
        self._opener = opener or urllib.request.urlopen

    def list_events(
        self,
        *,
        calendar_id: str,
        sync_token: str | None,
        page_token: str | None,
    ) -> GoogleCalendarPage:
        token = self._access_token_provider()
        if not token:
            raise GoogleCalendarAuthError("missing Google Calendar access token")
        query: dict[str, str] = {
            "maxResults": "2500",
            "showDeleted": "true",
        }
        if sync_token is not None:
            query["syncToken"] = sync_token
        if page_token is not None:
            query["pageToken"] = page_token
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{encoded_calendar}/events?{urllib.parse.urlencode(query)}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "student-execution-os/0",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            if exc.code == 410:
                raise GoogleCalendarInvalidSyncToken(
                    body or "Google Calendar sync token is invalid"
                ) from exc
            if exc.code in (401, 403):
                raise GoogleCalendarAuthError(
                    body or f"Google Calendar authorization failed: {exc.code}"
                ) from exc
            if exc.code == 429 or exc.code >= 500:
                raise GoogleCalendarTransientError(
                    body or f"Google Calendar transient HTTP error: {exc.code}"
                ) from exc
            raise GoogleCalendarProviderError(
                body or f"Google Calendar HTTP error: {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise GoogleCalendarTransientError(str(exc.reason)) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleCalendarProtocolError(
                "Google Calendar returned invalid JSON"
            ) from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
            raise GoogleCalendarProtocolError(
                "Google Calendar Events.list response has invalid shape"
            )
        return GoogleCalendarPage(
            items=tuple(item for item in payload.get("items", []) if isinstance(item, dict)),
            next_page_token=payload.get("nextPageToken"),
            next_sync_token=payload.get("nextSyncToken"),
        )


class GoogleCalendarConnector:
    def __init__(
        self,
        *,
        account_id: str,
        connector_id: str,
        source_system_id: str,
        calendar_id: str,
        transport: GoogleCalendarTransport,
        reconciliation: SQLiteReconciliationRepository,
        connectors: SQLiteConnectorRepository,
        max_retries: int = 2,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if not calendar_id:
            raise ValidationError("calendar_id is required")
        self.account_id = account_id
        self.connector_id = connector_id
        self.source_system_id = source_system_id
        self.calendar_id = calendar_id
        self.transport = transport
        self.reconciliation = reconciliation
        self.connectors = connectors
        self.max_retries = max_retries
        self.sleeper = sleeper
        scope = "calendar:" + hashlib.sha256(calendar_id.encode("utf-8")).hexdigest()[:24]
        state = connectors.register(
            account_id=account_id,
            connector_id=connector_id,
            source_system_id=source_system_id,
            provider=_PROVIDER,
            scope=scope,
            connector_version=_CONNECTOR_VERSION,
        )
        if state.provider != _PROVIDER:
            raise ValidationError("connector provider mismatch")

    def sync(self) -> ConnectorSyncResult:
        state = self.connectors.get_state(self.account_id, self.connector_id)
        if state.checkpoint is None:
            return self._run_once(
                sync_token=None,
                full_sync=True,
                full_resync_performed=False,
            )
        try:
            return self._run_once(
                sync_token=state.checkpoint,
                full_sync=False,
                full_resync_performed=False,
            )
        except GoogleCalendarInvalidSyncToken:
            return self._run_once(
                sync_token=None,
                full_sync=True,
                full_resync_performed=True,
            )

    def _run_once(
        self,
        *,
        sync_token: str | None,
        full_sync: bool,
        full_resync_performed: bool,
    ) -> ConnectorSyncResult:
        session = self.connectors.start_session(
            account_id=self.account_id,
            connector_id=self.connector_id,
            is_full_sync=full_sync,
        )
        page_token: str | None = None
        page_count = 0
        record_count = 0
        deletion_count = 0
        seen_ids: set[str] = set()

        try:
            while True:
                page = self._fetch_page(
                    sync_token=sync_token,
                    page_token=page_token,
                )
                page_count += 1
                for item in page.items:
                    external_id = item.get("id")
                    if not isinstance(external_id, str) or not external_id:
                        raise GoogleCalendarProtocolError(
                            "Google Calendar event is missing id"
                        )
                    seen_ids.add(external_id)
                    ingested, removed = self._ingest_event(item)
                    record_count += 1 if ingested else 0
                    deletion_count += 1 if removed else 0
                if page.next_page_token is not None:
                    if page.next_sync_token is not None:
                        raise GoogleCalendarProtocolError(
                            "nextSyncToken must only appear on the terminal page"
                        )
                    page_token = page.next_page_token
                    continue
                if not page.next_sync_token:
                    raise GoogleCalendarProtocolError(
                        "terminal Google Calendar page is missing nextSyncToken"
                    )
                checkpoint_after = page.next_sync_token
                break

            if full_sync:
                known_active = self.connectors.active_external_entity_ids(
                    self.account_id,
                    self.connector_id,
                )
                for external_id in sorted(known_active - seen_ids):
                    if self._record_snapshot_absence(
                        external_id=external_id,
                        checkpoint_after=checkpoint_after,
                    ):
                        deletion_count += 1

            completed = self.connectors.finish_complete(
                account_id=self.account_id,
                session_id=session.id,
                checkpoint_after=checkpoint_after,
                page_count=page_count,
                record_count=record_count,
                deletion_count=deletion_count,
            )
            state = self.connectors.get_state(
                self.account_id,
                self.connector_id,
            )
            return ConnectorSyncResult(
                session=completed,
                checkpoint=state.checkpoint,
                health=state.health,
                full_resync_performed=full_resync_performed,
            )
        except GoogleCalendarInvalidSyncToken:
            failed, invalidated = self.connectors.finish_invalid_cursor(
                account_id=self.account_id,
                session_id=session.id,
                page_count=page_count,
                record_count=record_count,
                deletion_count=deletion_count,
            )
            if invalidated:
                raise
            state = self.connectors.get_state(
                self.account_id,
                self.connector_id,
            )
            return ConnectorSyncResult(
                session=failed,
                checkpoint=state.checkpoint,
                health=state.health,
                full_resync_performed=full_resync_performed,
            )
        except GoogleCalendarAuthError:
            failed = self.connectors.finish_failure(
                account_id=self.account_id,
                session_id=session.id,
                error_code=GoogleCalendarAuthError.code,
                page_count=page_count,
                record_count=record_count,
                deletion_count=deletion_count,
                unavailable=True,
            )
            state = self.connectors.get_state(
                self.account_id,
                self.connector_id,
            )
            return ConnectorSyncResult(
                session=failed,
                checkpoint=state.checkpoint,
                health=state.health,
                full_resync_performed=full_resync_performed,
            )
        except GoogleCalendarProviderError as exc:
            failed = self.connectors.finish_failure(
                account_id=self.account_id,
                session_id=session.id,
                error_code=getattr(
                    exc,
                    "code",
                    GoogleCalendarProviderError.code,
                ),
                page_count=page_count,
                record_count=record_count,
                deletion_count=deletion_count,
                unavailable=False,
            )
            state = self.connectors.get_state(
                self.account_id,
                self.connector_id,
            )
            return ConnectorSyncResult(
                session=failed,
                checkpoint=state.checkpoint,
                health=state.health,
                full_resync_performed=full_resync_performed,
            )

    def _fetch_page(
        self,
        *,
        sync_token: str | None,
        page_token: str | None,
    ) -> GoogleCalendarPage:
        attempt = 0
        while True:
            try:
                return self.transport.list_events(
                    calendar_id=self.calendar_id,
                    sync_token=sync_token,
                    page_token=page_token,
                )
            except GoogleCalendarTransientError:
                if attempt >= self.max_retries:
                    raise
                self.sleeper(0.25 * (2**attempt))
                attempt += 1

    def _ingest_event(
        self,
        item: Mapping[str, Any],
    ) -> tuple[bool, bool]:
        external_id = str(item["id"])
        content_hash = hashlib.sha256(
            _canonical_json(item).encode("utf-8")
        ).hexdigest()
        updated = item.get("updated")
        provider_updated = None
        source_revision = None
        revision_order = None
        if isinstance(updated, str) and updated:
            provider_updated = _parse_provider_time(updated)
            source_revision = self._provider_revision(item)
            revision_order = int(provider_updated.timestamp() * 1_000_000)
            receipt_revision = source_revision
        elif item.get("status") == "cancelled":
            receipt_revision = f"deleted-content:{content_hash}"
        else:
            raise GoogleCalendarProtocolError(
                "active Google Calendar event is missing updated revision"
            )
        if self.connectors.has_receipt(
            account_id=self.account_id,
            connector_id=self.connector_id,
            external_entity_id=external_id,
            provider_revision=receipt_revision,
        ):
            return False, False

        observed_at = self.reconciliation.clock.now()
        record_id = _stable_id(
            "google-calendar-record",
            self.connector_id,
            external_id,
            receipt_revision,
        )

        if item.get("status") == "cancelled":
            observation_id = _stable_id(
                "google-calendar-removal",
                record_id,
            )
            removal = self.reconciliation.record_source_removal(
                account_id=self.account_id,
                source_system_id=self.source_system_id,
                external_entity_id=external_id,
                extractor_id=_EXTRACTOR_ID,
                actor=ActorCategory.CONNECTOR_INGESTION,
                source_revision=source_revision,
                revision_order=revision_order,
                source_record_id=record_id,
                observation_id=observation_id,
                observed_at=observed_at,
                content_hash=content_hash,
                metadata={
                    "provider": _PROVIDER,
                    "provider_updated": provider_updated.isoformat() if provider_updated else None,
                    "etag": item.get("etag"),
                    "status": "cancelled",
                    "connector_version": _CONNECTOR_VERSION,
                    "deletion_semantics": "explicit_cancelled_event",
                    "recurring_event_id": item.get("recurringEventId"),
                    "original_start_time": item.get("originalStartTime"),
                },
            )
            source_record_id = removal.source_record_id
            entity_state = ConnectorEntityState.REMOVED
            removed = True
        else:
            try:
                record = self.reconciliation.get_source_record(
                    self.account_id,
                    record_id,
                )
            except EntityNotFound:
                record = self.reconciliation.add_source_record(
                    account_id=self.account_id,
                    source_system_id=self.source_system_id,
                    external_entity_id=external_id,
                    source_revision=source_revision,
                    revision_order=revision_order,
                    observed_at=observed_at,
                    content_hash=content_hash,
                    metadata={
                        "provider": _PROVIDER,
                        "provider_updated": provider_updated.isoformat() if provider_updated else None,
                        "etag": item.get("etag"),
                        "status": item.get("status"),
                        "connector_version": _CONNECTOR_VERSION,
                    },
                    actor=ActorCategory.CONNECTOR_INGESTION,
                    source_record_id=record_id,
                )
            binding_id = self.connectors.active_binding_id(
                account_id=self.account_id,
                source_system_id=self.source_system_id,
                external_entity_id=external_id,
            )
            self._ensure_string_observation(
                record_id=record.id,
                binding_id=binding_id,
                suffix="status",
                field_path="calendar.status",
                value=str(item.get("status", "confirmed")),
            )
            for suffix, field_path, value in (
                ("summary", "calendar.summary", item.get("summary")),
                ("start", "calendar.start", self._event_time(item.get("start"))),
                ("end", "calendar.end", self._event_time(item.get("end"))),
                (
                    "event-type",
                    "calendar.event_type",
                    item.get("eventType", "default"),
                ),
            ):
                if value is not None:
                    self._ensure_string_observation(
                        record_id=record.id,
                        binding_id=binding_id,
                        suffix=suffix,
                        field_path=field_path,
                        value=str(value),
                    )
            source_record_id = record.id
            entity_state = ConnectorEntityState.ACTIVE
            removed = False

        self.connectors.record_receipt(
            account_id=self.account_id,
            connector_id=self.connector_id,
            external_entity_id=external_id,
            provider_revision=receipt_revision,
            source_record_id=source_record_id,
            state=entity_state,
        )
        return True, removed

    def _ensure_string_observation(
        self,
        *,
        record_id: str,
        binding_id: str | None,
        suffix: str,
        field_path: str,
        value: str,
    ) -> None:
        observation_id = _stable_id(
            "google-calendar-observation",
            record_id,
            suffix,
        )
        if self.reconciliation.observation_exists(
            self.account_id,
            observation_id,
        ):
            return
        self.reconciliation.add_observation(
            account_id=self.account_id,
            source_record_id=record_id,
            binding_id=binding_id,
            field_path=field_path,
            value_type=ObservationValueType.STRING,
            value=value,
            extraction_certainty=ExtractionCertainty.EXACT,
            extractor_id=_EXTRACTOR_ID,
            actor=ActorCategory.CONNECTOR_INGESTION,
            observation_id=observation_id,
        )

    def _record_snapshot_absence(
        self,
        *,
        external_id: str,
        checkpoint_after: str,
    ) -> bool:
        provider_revision = "fullsync-absence:" + hashlib.sha256(
            f"{checkpoint_after}|{external_id}".encode("utf-8")
        ).hexdigest()
        if self.connectors.has_receipt(
            account_id=self.account_id,
            connector_id=self.connector_id,
            external_entity_id=external_id,
            provider_revision=provider_revision,
        ):
            return False
        now = self.reconciliation.clock.now()
        record_id = _stable_id(
            "google-calendar-record",
            self.connector_id,
            external_id,
            provider_revision,
        )
        observation_id = _stable_id(
            "google-calendar-removal",
            record_id,
        )
        removal = self.reconciliation.record_source_removal(
            account_id=self.account_id,
            source_system_id=self.source_system_id,
            external_entity_id=external_id,
            extractor_id=_EXTRACTOR_ID,
            actor=ActorCategory.CONNECTOR_INGESTION,
            source_revision=None,
            revision_order=None,
            source_record_id=record_id,
            observation_id=observation_id,
            observed_at=now,
            metadata={
                "provider": _PROVIDER,
                "deletion_semantics": "complete_full_snapshot_absence",
            },
        )
        self.connectors.record_receipt(
            account_id=self.account_id,
            connector_id=self.connector_id,
            external_entity_id=external_id,
            provider_revision=provider_revision,
            source_record_id=removal.source_record_id,
            state=ConnectorEntityState.REMOVED,
        )
        return True

    @staticmethod
    def _provider_revision(item: Mapping[str, Any]) -> str:
        updated = item.get("updated")
        if not isinstance(updated, str) or not updated:
            raise GoogleCalendarProtocolError(
                "Google Calendar event is missing updated revision"
            )
        etag = item.get("etag")
        return updated if not etag else f"{updated}|{etag}"

    @staticmethod
    def _event_time(value: Any) -> str | None:
        if not isinstance(value, Mapping):
            return None
        if isinstance(value.get("dateTime"), str):
            return value["dateTime"]
        if isinstance(value.get("date"), str):
            return value["date"]
        return None
