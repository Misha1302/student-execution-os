"""Connection lifecycle for provider-independent academic schedules.

Fetching is intentionally separate from applying.  A failed fetch or parse leaves the
last canonical snapshot untouched; a successful result, connector checkpoint, and
connection status commit in one SQLite write transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.agent.credentials import CredentialCipher, CredentialUnreadable
from student_execution_os.connectors import ConnectorSessionStatus, SQLiteConnectorRepository
from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.recurrence.source import SourceApplier, SourceSnapshot

from .credentials import feed_aad
from .http import HttpIcsReader, assert_public_feed_url
from .ical import ICalendarAcademicProvider, parse_icalendar
from .model import AcademicProviderError, AcademicProviderResult

PROVIDER = "ACADEMIC_ICAL"
CONNECTOR_VERSION = "ical-rfc5545-v1"
SOURCE_KIND = "ACADEMIC_SCHEDULE"
_UNSET = object()


def academic_connector_id(account_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"botay:academic-ical-connector:{account_id}"))


def academic_source_id(account_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"botay:academic-schedule-source:{account_id}"))


def _timezone(value: str) -> str:
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("default_timezone must be a known IANA timezone") from exc
    return name


class AcademicScheduleService:
    """Own one primary academic feed for an account; never writes classes directly."""

    def __init__(
        self,
        repo: SQLiteCanonicalRepository,
        *,
        account_id: str,
        cipher: CredentialCipher | None = None,
        reader_factory: Callable[[str], Callable[[], bytes]] = HttpIcsReader,
        url_validator: Callable[[str], str] = assert_public_feed_url,
    ) -> None:
        self.repo = repo
        self.account_id = account_id
        self.cipher = cipher
        self.reader_factory = reader_factory
        self.url_validator = url_validator
        self.reconciliation = SQLiteReconciliationRepository(repo)
        self.connectors = SQLiteConnectorRepository(repo, self.reconciliation)
        self.connector_id = academic_connector_id(account_id)
        self.source_system_id = academic_source_id(account_id)

    def _ensure_owner(self) -> None:
        self.repo._require_account(self.account_id)
        try:
            self.reconciliation.get_source_system(self.account_id, self.source_system_id)
        except EntityNotFound:
            self.reconciliation.create_source_system(
                account_id=self.account_id,
                kind=SOURCE_KIND,
                actor=ActorCategory.USER_UI,
                source_system_id=self.source_system_id,
                policy_context={"provider_contract": "AcademicScheduleProvider/v1"},
            )
        self.connectors.register(
            account_id=self.account_id,
            connector_id=self.connector_id,
            source_system_id=self.source_system_id,
            provider=PROVIDER,
            scope="primary",
            connector_version=CONNECTOR_VERSION,
        )

    def _row(self):
        return self.repo.connection.execute(
            "SELECT * FROM academic_schedule_connections WHERE account_id=?",
            (self.account_id,),
        ).fetchone()

    def status(self) -> dict[str, Any]:
        self.repo._require_account(self.account_id)
        row = self._row()
        if row is None:
            return {
                "connected": False,
                "mode": None,
                "display_name": None,
                "default_timezone": None,
                "feed_host": None,
                "sync_interval_minutes": None,
                "next_sync_at": None,
                "last_content_sha256": None,
                "health": None,
                "last_successful_sync_at": None,
                "latest_failure_reason": None,
                "last_attempt_at": None,
                "last_attempt_status": None,
                "live_hse_validated": False,
            }
        state = self.connectors.get_state(self.account_id, row["connector_id"])
        latest = self.repo.connection.execute(
            "SELECT started_at,status FROM connector_sync_sessions WHERE account_id=? AND connector_id=? "
            "ORDER BY started_at DESC,id DESC LIMIT 1",
            (self.account_id, row["connector_id"]),
        ).fetchone()
        return {
            "connected": row["mode"] != "DISCONNECTED",
            "mode": row["mode"],
            "display_name": row["display_name"],
            "default_timezone": row["default_timezone"],
            # A hostname is useful diagnostics; the bearer-capability URL never leaves storage.
            "feed_host": row["feed_host"],
            "sync_interval_minutes": int(row["sync_interval_minutes"]),
            "next_sync_at": row["next_sync_at"],
            "last_content_sha256": row["last_content_sha256"],
            "health": state.health.value,
            "last_successful_sync_at": _iso(state.last_successful_complete_sync_at),
            "latest_failure_reason": state.latest_failure_reason,
            "last_attempt_at": None if latest is None else latest["started_at"],
            "last_attempt_status": None if latest is None else latest["status"],
            "live_hse_validated": False,
        }

    def connect_url(
        self,
        *,
        url: str,
        display_name: str = "Academic calendar",
        default_timezone: str = "Europe/Moscow",
        sync_interval_minutes: int = 60,
    ) -> dict[str, Any]:
        if self.cipher is None:
            raise ValidationError("academic feed encryption key is not configured")
        safe_url = self.url_validator(url)
        timezone_name = _timezone(default_timezone)
        name = display_name.strip()
        if not 1 <= len(name) <= 120:
            raise ValidationError("display_name must contain 1 to 120 characters")
        interval = int(sync_interval_minutes)
        if not 15 <= interval <= 1440:
            raise ValidationError("sync_interval_minutes must be between 15 and 1440")
        nonce, ciphertext, key_id = self.cipher.encrypt(
            safe_url, feed_aad(self.account_id, self.connector_id)
        )
        self._ensure_owner()
        now = self.repo.clock.now()
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO academic_schedule_connections("
                "account_id,connector_id,source_system_id,display_name,mode,default_timezone,"
                "feed_ciphertext,feed_nonce,feed_key_id,feed_host,sync_interval_minutes,next_sync_at,"
                "last_content_sha256,created_at,updated_at,version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,1) "
                "ON CONFLICT(account_id) DO UPDATE SET display_name=excluded.display_name,mode='URL',"
                "default_timezone=excluded.default_timezone,feed_ciphertext=excluded.feed_ciphertext,"
                "feed_nonce=excluded.feed_nonce,feed_key_id=excluded.feed_key_id,feed_host=excluded.feed_host,"
                "sync_interval_minutes=excluded.sync_interval_minutes,next_sync_at=excluded.next_sync_at,"
                "updated_at=excluded.updated_at,version=academic_schedule_connections.version+1",
                (
                    self.account_id,
                    self.connector_id,
                    self.source_system_id,
                    name,
                    "URL",
                    timezone_name,
                    ciphertext,
                    nonce,
                    key_id,
                    urlsplit(safe_url).hostname,
                    interval,
                    _iso(now),
                    _iso(now),
                    _iso(now),
                ),
            )
        return self.refresh()

    def import_ics(
        self,
        content: bytes,
        *,
        display_name: str = "Imported academic calendar",
        default_timezone: str = "Europe/Moscow",
    ) -> dict[str, Any]:
        timezone_name = _timezone(default_timezone)
        name = display_name.strip()
        if not 1 <= len(name) <= 120:
            raise ValidationError("display_name must contain 1 to 120 characters")
        # Parse first: a malformed upload must not replace a working connection.
        result = parse_icalendar(
            content,
            source_system_id=self.source_system_id,
            default_timezone=timezone_name,
            complete=True,
        )
        self._ensure_owner()
        row = self._row()
        return self._apply_result(
            result,
            replace_with_upload=(name, timezone_name),
            connection_version_before=None if row is None else int(row["version"]),
        )

    def refresh(self) -> dict[str, Any]:
        row = self._row()
        if row is None or row["mode"] != "URL":
            raise ValidationError("no refreshable academic calendar URL is connected")
        session = self.connectors.start_session(
            account_id=self.account_id,
            connector_id=row["connector_id"],
            is_full_sync=True,
        )
        if self.cipher is None:
            self._finish_failure(
                session.id, "CREDENTIAL_UNREADABLE", connection_version=int(row["version"])
            )
            raise AcademicProviderError("academic feed encryption key is unavailable", "CREDENTIAL_UNREADABLE")
        try:
            url = self.cipher.decrypt(
                row["feed_nonce"],
                row["feed_ciphertext"],
                row["feed_key_id"],
                feed_aad(self.account_id, row["connector_id"]),
            )
        except CredentialUnreadable as exc:
            self._finish_failure(
                session.id, "CREDENTIAL_UNREADABLE", connection_version=int(row["version"])
            )
            raise AcademicProviderError(
                "academic feed credential cannot be decrypted", "CREDENTIAL_UNREADABLE"
            ) from exc
        try:
            result = ICalendarAcademicProvider(
                source_system_id=row["source_system_id"],
                default_timezone=row["default_timezone"],
                reader=self.reader_factory(url),
                complete=True,
            ).fetch()
        except AcademicProviderError as exc:
            self._finish_failure(session.id, exc.code, connection_version=int(row["version"]))
            raise
        return self._apply_result(
            result, session_id=session.id, connection_version_before=int(row["version"])
        )

    def _finish_failure(
        self, session_id: str, error_code: str, *, connection_version: int | None = None
    ) -> None:
        unavailable = error_code in {
            "AUTH_REQUIRED",
            "NOT_FOUND",
            "CREDENTIAL_UNREADABLE",
            "BLOCKED_URL",
        }
        session = self.connectors.get_session(self.account_id, session_id)
        with self.repo._tx() as conn:
            before_state = self.connectors.get_state(self.account_id, session.connector_id)
            self.connectors.finish_failure(
                account_id=self.account_id,
                session_id=session_id,
                error_code=error_code,
                page_count=0,
                record_count=0,
                deletion_count=0,
                unavailable=unavailable,
            )
            state = self.connectors.get_state(self.account_id, session.connector_id)
            won_state_update = (
                before_state.version == session.state_version_before
                and state.version == session.state_version_before + 1
                and state.latest_failure_reason == error_code
            )
            row = self._row()
            if (
                won_state_update
                and row is not None
                and row["mode"] == "URL"
                and (connection_version is None or int(row["version"]) == connection_version)
            ):
                now = self.repo.clock.now()
                retry_at = now + timedelta(minutes=int(row["sync_interval_minutes"]))
                conn.execute(
                    "UPDATE academic_schedule_connections SET next_sync_at=?,updated_at=?,version=version+1 "
                    "WHERE account_id=? AND mode='URL' AND version=?",
                    (_iso(retry_at), _iso(now), self.account_id, int(row["version"])),
                )

    def _apply_result(
        self,
        result: AcademicProviderResult,
        *,
        session_id: str | None = None,
        remove_connection: bool = False,
        replace_with_upload: tuple[str, str] | None = None,
        connection_version_before: int | None | object = _UNSET,
    ) -> dict[str, Any]:
        session = (
            self.connectors.start_session(
                account_id=self.account_id,
                connector_id=self.connector_id,
                is_full_sync=result.snapshot.complete,
            )
            if session_id is None
            else self.connectors.get_session(self.account_id, session_id)
        )
        conflict = False
        report = None
        try:
            with self.repo._tx() as conn:
                state = self.connectors.get_state(self.account_id, session.connector_id)
                connection_row = self._row()
                connection_version_matches = (
                    connection_version_before is _UNSET
                    or (
                        connection_version_before is None
                        and connection_row is None
                    )
                    or (
                        connection_row is not None
                        and int(connection_row["version"]) == connection_version_before
                    )
                )
                if state.version != session.state_version_before or not connection_version_matches:
                    conflict = True
                else:
                    report = SourceApplier(self.repo, account_id=self.account_id).apply(result.snapshot)
                    deletion_count = len(report.removed)
                    finished = self.connectors.finish_complete(
                        account_id=self.account_id,
                        session_id=session.id,
                        checkpoint_after=result.content_sha256,
                        page_count=1,
                        record_count=len(result.snapshot.series) + len(result.snapshot.events),
                        deletion_count=deletion_count,
                    )
                    if finished.status is not ConnectorSessionStatus.COMPLETE:
                        raise RuntimeError("academic connector checkpoint commit conflicted")
                    row = self._row()
                    next_sync = None
                    if row is not None and row["mode"] == "URL":
                        next_sync = self.repo.clock.now() + timedelta(
                            minutes=int(row["sync_interval_minutes"])
                        )
                    if remove_connection:
                        conn.execute(
                            "DELETE FROM academic_schedule_connections WHERE account_id=?",
                            (self.account_id,),
                        )
                    elif replace_with_upload is not None:
                        name, timezone_name = replace_with_upload
                        now = self.repo.clock.now()
                        conn.execute(
                            "INSERT INTO academic_schedule_connections("
                            "account_id,connector_id,source_system_id,display_name,mode,default_timezone,"
                            "feed_ciphertext,feed_nonce,feed_key_id,feed_host,sync_interval_minutes,next_sync_at,"
                            "last_content_sha256,created_at,updated_at,version) "
                            "VALUES (?,?,?,?,?, ?,NULL,NULL,NULL,NULL,60,NULL,?,?,?,1) "
                            "ON CONFLICT(account_id) DO UPDATE SET display_name=excluded.display_name,mode='UPLOAD',"
                            "default_timezone=excluded.default_timezone,feed_ciphertext=NULL,feed_nonce=NULL,"
                            "feed_key_id=NULL,feed_host=NULL,next_sync_at=NULL,last_content_sha256=excluded.last_content_sha256,"
                            "updated_at=excluded.updated_at,version=academic_schedule_connections.version+1",
                            (
                                self.account_id,
                                self.connector_id,
                                self.source_system_id,
                                name,
                                "UPLOAD",
                                timezone_name,
                                result.content_sha256,
                                _iso(now),
                                _iso(now),
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE academic_schedule_connections SET last_content_sha256=?,next_sync_at=?,"
                            "updated_at=?,version=version+1 WHERE account_id=?",
                            (
                                result.content_sha256,
                                _iso(next_sync),
                                _iso(self.repo.clock.now()),
                                self.account_id,
                            ),
                        )
        except ValidationError:
            self._finish_failure(
                session.id,
                "CANONICAL_APPLY_REJECTED",
                connection_version=(
                    connection_version_before
                    if isinstance(connection_version_before, int)
                    else None
                ),
            )
            raise
        if conflict:
            self._finish_failure(
                session.id,
                "CONCURRENT_SYNC_CONFLICT",
                connection_version=(
                    connection_version_before
                    if isinstance(connection_version_before, int)
                    else None
                ),
            )
            raise AcademicProviderError(
                "a newer academic refresh completed first", "CONCURRENT_SYNC_CONFLICT"
            )
        assert report is not None
        payload = self.status()
        payload["apply_report"] = report.as_dict()
        payload["component_count"] = result.component_count
        payload["diagnostics"] = list(result.diagnostics)
        return payload

    def disconnect(self) -> dict[str, Any]:
        row = self._row()
        if row is None or row["mode"] == "DISCONNECTED":
            return self.status()
        session = self.connectors.start_session(
            account_id=self.account_id,
            connector_id=row["connector_id"],
            is_full_sync=True,
        )
        empty = AcademicProviderResult(
            snapshot=SourceSnapshot(
                source_system_id=row["source_system_id"], series=(), events=(), complete=True
            ),
            content_sha256="disconnected",
            component_count=0,
        )
        return self._apply_result(
            empty,
            session_id=session.id,
            remove_connection=True,
            connection_version_before=int(row["version"]),
        )


def refresh_due_academic_schedules(
    database: str,
    *,
    cipher: CredentialCipher | None,
    now: datetime | None = None,
    reader_factory: Callable[[str], Callable[[], bytes]] = HttpIcsReader,
) -> dict[str, int]:
    """Refresh due URL connections; one account failure never blocks the next."""
    counts = {"due": 0, "complete": 0, "failed": 0}
    with SQLiteCanonicalRepository(database) as repo:
        repo.initialize()
        threshold = now or repo.clock.now()
        accounts = [
            row[0]
            for row in repo.connection.execute(
                "SELECT account_id FROM academic_schedule_connections "
                "WHERE mode='URL' AND next_sync_at<=? ORDER BY account_id",
                (_iso(threshold),),
            ).fetchall()
        ]
        counts["due"] = len(accounts)
        for account_id in accounts:
            try:
                AcademicScheduleService(
                    repo, account_id=account_id, cipher=cipher, reader_factory=reader_factory
                ).refresh()
            except (AcademicProviderError, ValidationError):
                counts["failed"] += 1
            else:
                counts["complete"] += 1
    return counts


__all__ = [
    "AcademicScheduleService",
    "academic_connector_id",
    "academic_source_id",
    "refresh_due_academic_schedules",
]
