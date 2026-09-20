from __future__ import annotations

from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.reconciliation import SourceAvailability, SQLiteReconciliationRepository

from .model import (
    ConnectorEntityState,
    ConnectorHealth,
    ConnectorSessionStatus,
    ConnectorState,
    ConnectorSyncSession,
)


class SQLiteConnectorRepository:
    """Persisted connector workflow state; evidence stays owned by reconciliation."""

    def __init__(
        self,
        canonical: SQLiteCanonicalRepository,
        reconciliation: SQLiteReconciliationRepository,
    ) -> None:
        self.canonical = canonical
        self.reconciliation = reconciliation
        self.connection = canonical.connection
        self.clock = canonical.clock

    def register(
        self,
        *,
        account_id: str,
        connector_id: str,
        source_system_id: str,
        provider: str,
        scope: str,
        connector_version: str,
    ) -> ConnectorState:
        self.canonical._require_account(account_id)
        self.reconciliation.get_source_system(account_id, source_system_id)
        if not all((connector_id, provider, scope, connector_version)):
            raise ValidationError("connector identity/provider/scope/version are required")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            row = conn.execute(
                "SELECT id FROM connector_states WHERE account_id=? AND id=?",
                (account_id, connector_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO connector_states("
                    "id,account_id,source_system_id,provider,scope,checkpoint,health_status,"
                    "last_successful_complete_sync_at,latest_failure_reason,connector_version,updated_at,version"
                    ") VALUES (?,?,?,?,?,NULL,'STALE',NULL,NULL,?,?,1)",
                    (
                        connector_id,
                        account_id,
                        source_system_id,
                        provider,
                        scope,
                        connector_version,
                        _iso(now),
                    ),
                )
            else:
                current = self.get_state(account_id, connector_id)
                expected = (source_system_id, provider, scope, connector_version)
                actual = (
                    current.source_system_id,
                    current.provider,
                    current.scope,
                    current.connector_version,
                )
                if actual != expected:
                    raise ValidationError(
                        "connector registration conflicts with existing immutable identity"
                    )
        return self.get_state(account_id, connector_id)

    def get_state(self, account_id: str, connector_id: str) -> ConnectorState:
        row = self.connection.execute(
            "SELECT * FROM connector_states WHERE account_id=? AND id=?",
            (account_id, connector_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("connector not found")
        return ConnectorState(
            id=row["id"],
            account_id=row["account_id"],
            source_system_id=row["source_system_id"],
            provider=row["provider"],
            scope=row["scope"],
            checkpoint=row["checkpoint"],
            health=ConnectorHealth(row["health_status"]),
            last_successful_complete_sync_at=_dt(row["last_successful_complete_sync_at"]),
            latest_failure_reason=row["latest_failure_reason"],
            connector_version=row["connector_version"],
            version=int(row["version"]),
        )

    def start_session(
        self,
        *,
        account_id: str,
        connector_id: str,
        is_full_sync: bool,
        session_id: str | None = None,
    ) -> ConnectorSyncSession:
        state = self.get_state(account_id, connector_id)
        sid = session_id or str(uuid4())
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO connector_sync_sessions("
                "id,account_id,connector_id,scope,cursor_before,cursor_after,is_full_sync,status,"
                "started_at,completed_at,error_code,page_count,record_count,deletion_count"
                ") VALUES (?,?,?,?,?,?,?,'FAILED',?,NULL,NULL,0,0,0)",
                (
                    sid,
                    account_id,
                    connector_id,
                    state.scope,
                    state.checkpoint,
                    None,
                    1 if is_full_sync else 0,
                    _iso(now),
                ),
            )
        return self.get_session(account_id, sid)

    def get_session(self, account_id: str, session_id: str) -> ConnectorSyncSession:
        row = self.connection.execute(
            "SELECT * FROM connector_sync_sessions WHERE account_id=? AND id=?",
            (account_id, session_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("connector sync session not found")
        return ConnectorSyncSession(
            id=row["id"],
            account_id=row["account_id"],
            connector_id=row["connector_id"],
            scope=row["scope"],
            cursor_before=row["cursor_before"],
            cursor_after=row["cursor_after"],
            is_full_sync=bool(row["is_full_sync"]),
            status=ConnectorSessionStatus(row["status"]),
            started_at=_dt(row["started_at"]),
            completed_at=_dt(row["completed_at"]),
            error_code=row["error_code"],
            page_count=int(row["page_count"]),
            record_count=int(row["record_count"]),
            deletion_count=int(row["deletion_count"]),
        )

    def list_sessions(self, account_id: str, connector_id: str) -> list[ConnectorSyncSession]:
        rows = self.connection.execute(
            "SELECT id FROM connector_sync_sessions WHERE account_id=? AND connector_id=? "
            "ORDER BY started_at,id",
            (account_id, connector_id),
        ).fetchall()
        return [self.get_session(account_id, row["id"]) for row in rows]

    def finish_complete(
        self,
        *,
        account_id: str,
        session_id: str,
        checkpoint_after: str,
        page_count: int,
        record_count: int,
        deletion_count: int,
    ) -> ConnectorSyncSession:
        if not checkpoint_after:
            raise ValidationError("complete connector session requires a checkpoint")
        session = self.get_session(account_id, session_id)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE connector_sync_sessions SET status='COMPLETE',cursor_after=?,completed_at=?,"
                "error_code=NULL,page_count=?,record_count=?,deletion_count=? "
                "WHERE account_id=? AND id=?",
                (
                    checkpoint_after,
                    _iso(now),
                    page_count,
                    record_count,
                    deletion_count,
                    account_id,
                    session_id,
                ),
            )
            conn.execute(
                "UPDATE connector_states SET checkpoint=?,health_status='CURRENT',"
                "last_successful_complete_sync_at=?,latest_failure_reason=NULL,updated_at=?,"
                "version=version+1 WHERE account_id=? AND id=?",
                (
                    checkpoint_after,
                    _iso(now),
                    _iso(now),
                    account_id,
                    session.connector_id,
                ),
            )
        source_id = self.get_state(account_id, session.connector_id).source_system_id
        self.reconciliation.mark_source_availability(
            account_id=account_id,
            source_system_id=source_id,
            status=SourceAvailability.ACTIVE,
            actor=ActorCategory.CONNECTOR_INGESTION,
        )
        return self.get_session(account_id, session_id)

    def finish_failure(
        self,
        *,
        account_id: str,
        session_id: str,
        error_code: str,
        page_count: int,
        record_count: int,
        deletion_count: int,
        unavailable: bool,
    ) -> ConnectorSyncSession:
        session = self.get_session(account_id, session_id)
        status = (
            ConnectorSessionStatus.PARTIAL
            if page_count > 0
            else ConnectorSessionStatus.FAILED
        )
        health = (
            ConnectorHealth.UNAVAILABLE
            if unavailable
            else ConnectorHealth.STALE
        )
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE connector_sync_sessions SET status=?,completed_at=?,error_code=?,"
                "page_count=?,record_count=?,deletion_count=? WHERE account_id=? AND id=?",
                (
                    status.value,
                    _iso(now),
                    error_code,
                    page_count,
                    record_count,
                    deletion_count,
                    account_id,
                    session_id,
                ),
            )
            conn.execute(
                "UPDATE connector_states SET health_status=?,latest_failure_reason=?,updated_at=?,"
                "version=version+1 WHERE account_id=? AND id=?",
                (
                    health.value,
                    error_code,
                    _iso(now),
                    account_id,
                    session.connector_id,
                ),
            )
        source_status = (
            SourceAvailability.UNAVAILABLE
            if unavailable
            else SourceAvailability.STALE
        )
        source_id = self.get_state(account_id, session.connector_id).source_system_id
        self.reconciliation.mark_source_availability(
            account_id=account_id,
            source_system_id=source_id,
            status=source_status,
            actor=ActorCategory.CONNECTOR_INGESTION,
        )
        return self.get_session(account_id, session_id)

    def invalidate_checkpoint(
        self,
        *,
        account_id: str,
        connector_id: str,
        reason: str,
    ) -> None:
        state = self.get_state(account_id, connector_id)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE connector_states SET checkpoint=NULL,health_status='STALE',"
                "latest_failure_reason=?,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=?",
                (reason, _iso(now), account_id, connector_id),
            )
        self.reconciliation.mark_source_availability(
            account_id=account_id,
            source_system_id=state.source_system_id,
            status=SourceAvailability.STALE,
            actor=ActorCategory.CONNECTOR_INGESTION,
        )

    def has_receipt(
        self,
        *,
        account_id: str,
        connector_id: str,
        external_entity_id: str,
        provider_revision: str,
    ) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM connector_ingestion_receipts WHERE account_id=? AND connector_id=? "
            "AND external_entity_id=? AND provider_revision=?",
            (
                account_id,
                connector_id,
                external_entity_id,
                provider_revision,
            ),
        ).fetchone() is not None

    def record_receipt(
        self,
        *,
        account_id: str,
        connector_id: str,
        external_entity_id: str,
        provider_revision: str,
        source_record_id: str,
        state: ConnectorEntityState,
    ) -> None:
        now = self.clock.now()
        existing = self.connection.execute(
            "SELECT source_record_id FROM connector_ingestion_receipts "
            "WHERE account_id=? AND connector_id=? AND external_entity_id=? AND provider_revision=?",
            (account_id, connector_id, external_entity_id, provider_revision),
        ).fetchone()
        if existing is not None and existing["source_record_id"] != source_record_id:
            raise ValidationError(
                "connector receipt idempotency collision for provider revision"
            )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO connector_ingestion_receipts("
                "account_id,connector_id,external_entity_id,provider_revision,source_record_id,ingested_at"
                ") VALUES (?,?,?,?,?,?)",
                (
                    account_id,
                    connector_id,
                    external_entity_id,
                    provider_revision,
                    source_record_id,
                    _iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO connector_entities("
                "account_id,connector_id,external_entity_id,state,last_provider_revision,"
                "last_source_record_id,last_seen_at"
                ") VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,connector_id,external_entity_id) DO UPDATE SET "
                "state=excluded.state,last_provider_revision=excluded.last_provider_revision,"
                "last_source_record_id=excluded.last_source_record_id,last_seen_at=excluded.last_seen_at",
                (
                    account_id,
                    connector_id,
                    external_entity_id,
                    state.value,
                    provider_revision,
                    source_record_id,
                    _iso(now),
                ),
            )

    def active_external_entity_ids(
        self,
        account_id: str,
        connector_id: str,
    ) -> set[str]:
        rows = self.connection.execute(
            "SELECT external_entity_id FROM connector_entities "
            "WHERE account_id=? AND connector_id=? AND state='ACTIVE'",
            (account_id, connector_id),
        ).fetchall()
        return {row["external_entity_id"] for row in rows}

    def active_binding_id(
        self,
        *,
        account_id: str,
        source_system_id: str,
        external_entity_id: str,
    ) -> str | None:
        row = self.connection.execute(
            "SELECT id FROM source_bindings WHERE account_id=? AND source_system_id=? "
            "AND external_entity_id=? AND state='ACTIVE'",
            (account_id, source_system_id, external_entity_id),
        ).fetchone()
        return None if row is None else row["id"]
