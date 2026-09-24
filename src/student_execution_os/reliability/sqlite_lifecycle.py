from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from student_execution_os import __version__
from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository

BACKUP_FORMAT_VERSION = 1
ACCOUNT_EXPORT_FORMAT_VERSION = 1
_PRIVATE_FILE_MODE = 0o600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private(path: Path) -> None:
    if os.name == "posix":
        path.chmod(_PRIVATE_FILE_MODE)


def _temp_file(parent: Path, *, prefix: str) -> Path:
    fd, raw = tempfile.mkstemp(prefix=prefix, dir=parent)
    os.close(fd)
    path = Path(raw)
    _private(path)
    return path


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_file(path.parent, prefix=f".{path.name}.")
    try:
        temp.write_text(content, encoding="utf-8")
        _private(temp)
        os.replace(temp, path)
        _private(path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _database_path(database: str | Path) -> Path:
    value = str(database)
    if value == ":memory:" or value.startswith("file:"):
        raise ValidationError("reliability operations require a filesystem SQLite database")
    return Path(value)


def _schema_version(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def _integrity_check(connection: sqlite3.Connection) -> str:
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return "missing" if row is None else str(row[0])


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    application_version: str
    created_at: str
    database_filename: str
    schema_version: int
    sha256: str
    integrity_check: str
    account_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    destination: str
    backup_schema_version: int
    restored_schema_version: int
    sha256_verified: bool
    integrity_check: str
    foreign_key_violations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountDeletionPolicy:
    policy_version: str = "account-deletion-v1"
    tombstone_retention_days: int = 30
    retained_reason: str = "reject stale connector/client replay and account-id reuse during deletion propagation"

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")
        if self.tombstone_retention_days < 0:
            raise ValueError("tombstone_retention_days must be >= 0")
        if not self.retained_reason.strip():
            raise ValueError("retained_reason is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountDeletionResult:
    deletion_id: str
    account_id: str
    deleted_at: str
    purge_after: str
    policy_version: str
    retained_tombstone_fields: tuple[str, ...]
    deleted_rows: dict[str, int]
    secret_revocation_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountExport:
    format_version: int
    application_version: str
    schema_version: int
    exported_at: str
    account_id: str
    contract: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_DIRECT_ACCOUNT_TABLES = (
    "obligations",
    "projects",
    "project_members",
    "milestones",
    "dependencies",
    "user_time_constraints",
    "audit_changes",
    "source_systems",
    "source_status_history",
    "source_records",
    "source_bindings",
    "observations",
    "reconciliation_policies",
    "user_overrides",
    "reconciliation_conflicts",
    "effective_fields",
    "effective_field_history",
    "idempotency_records",
    "reconciliation_audit",
    "connector_states",
    "connector_sync_sessions",
    "connector_entities",
    "connector_ingestion_receipts",
    "action_intents",
    "action_idempotency_records",
    "action_intent_history",
    "places",
    "current_location_context",
    "travel_estimates",
    "plan_snapshots",
    "current_plans",
    "recurring_templates",
    "occurrence_overrides",
    "notifications",
    "notification_delivery_outbox",
)

_CHILD_TABLE_QUERIES: dict[str, str] = {
    "tasks": (
        "SELECT t.* FROM tasks t JOIN obligations o ON o.id=t.obligation_id "
        "WHERE o.account_id=? ORDER BY t.obligation_id"
    ),
    "events": (
        "SELECT e.* FROM events e JOIN obligations o ON o.id=e.obligation_id "
        "WHERE o.account_id=? ORDER BY e.obligation_id"
    ),
    "binding_history": (
        "SELECT h.* FROM binding_history h JOIN source_bindings b ON b.id=h.binding_id "
        "WHERE b.account_id=? ORDER BY h.id"
    ),
    "override_history": (
        "SELECT h.* FROM override_history h JOIN user_overrides o ON o.id=h.override_id "
        "WHERE o.account_id=? ORDER BY h.id"
    ),
    "conflict_history": (
        "SELECT h.* FROM conflict_history h JOIN reconciliation_conflicts c ON c.id=h.conflict_id "
        "WHERE c.account_id=? ORDER BY h.id"
    ),
    "plan_blocks": (
        "SELECT b.* FROM plan_blocks b JOIN plan_snapshots p ON p.id=b.plan_id "
        "WHERE p.account_id=? ORDER BY b.plan_id,b.starts_at,b.id"
    ),
}


# Login credentials and session token hashes are account-scoped and purged with the
# account, but they are never part of the user data export contract.
_ACCOUNT_CREDENTIAL_TABLES = (
    "auth_sessions",
    "auth_users",
)

_GLOBAL_LIFECYCLE_TABLES = {
    "schema_migrations",
    "account_deletion_tombstones",
}

_KNOWN_DATABASE_TABLES = {
    *_GLOBAL_LIFECYCLE_TABLES,
    "accounts",
    *_DIRECT_ACCOUNT_TABLES,
    *_ACCOUNT_CREDENTIAL_TABLES,
    *_CHILD_TABLE_QUERIES.keys(),
}


def _assert_lifecycle_schema_known(connection: sqlite3.Connection) -> None:
    actual = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    unknown = sorted(actual - _KNOWN_DATABASE_TABLES)
    if unknown:
        raise ValidationError(
            "data lifecycle contract does not classify database tables: " + ", ".join(unknown)
        )



def _rows(connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


class SQLiteDataLifecycle:
    """Consistent SQLite backup/restore and explicit account export.

    Backups are full-database operator artifacts because cross-table provenance,
    connector checkpoints, idempotency state and notification workflow must recover
    atomically. Account export is a separate user-data contract and is explicitly
    account-filtered rather than a database copy.
    """

    def __init__(self, database: str | Path, *, now=None) -> None:
        self.database = _database_path(database)
        self._now = now or _utc_now

    @staticmethod
    def manifest_path(backup: str | Path) -> Path:
        backup_path = Path(backup)
        return backup_path.with_name(f"{backup_path.name}.manifest.json")

    def create_backup(self, destination: str | Path) -> BackupManifest:
        source = self.database
        destination = Path(destination)
        if not source.exists():
            raise ValidationError("source database does not exist")
        if source.resolve() == destination.resolve():
            raise ValidationError("backup destination must differ from source database")
        if destination.exists():
            raise ValidationError("backup destination already exists")
        manifest_path = self.manifest_path(destination)
        if manifest_path.exists():
            raise ValidationError("backup manifest destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = _temp_file(destination.parent, prefix=f".{destination.name}.backup-")
        try:
            source_connection = sqlite3.connect(source)
            target_connection = sqlite3.connect(temp)
            try:
                source_connection.execute("PRAGMA query_only = ON")
                source_connection.backup(target_connection)
                target_connection.commit()
                integrity = _integrity_check(target_connection)
                if integrity != "ok":
                    raise RuntimeError(f"backup integrity check failed: {integrity}")
                schema_version = _schema_version(target_connection)
                account_count = int(target_connection.execute("SELECT count(*) FROM accounts").fetchone()[0])
            finally:
                target_connection.close()
                source_connection.close()
            _private(temp)
            os.replace(temp, destination)
            _private(destination)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            application_version=__version__,
            created_at=_iso(self._now()),
            database_filename=destination.name,
            schema_version=schema_version,
            sha256=_sha256(destination),
            integrity_check=integrity,
            account_count=account_count,
        )
        _atomic_text(manifest_path, json.dumps(manifest.to_dict(), sort_keys=True, indent=2) + "\n")
        return manifest

    @classmethod
    def restore_backup(
        cls,
        backup: str | Path,
        destination: str | Path,
        *,
        manifest: str | Path | None = None,
        overwrite: bool = False,
    ) -> RestoreResult:
        backup = Path(backup)
        destination = Path(destination)
        manifest_path = Path(manifest) if manifest is not None else cls.manifest_path(backup)
        if not backup.exists():
            raise ValidationError("backup database does not exist")
        if not manifest_path.exists():
            raise ValidationError("backup manifest does not exist")
        if backup.resolve() == destination.resolve():
            raise ValidationError("restore destination must differ from backup database")
        if destination.exists() and not overwrite:
            raise ValidationError("restore destination already exists; explicit overwrite is required")

        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        required = {
            "format_version",
            "application_version",
            "created_at",
            "database_filename",
            "schema_version",
            "sha256",
            "integrity_check",
            "account_count",
        }
        if set(raw) != required or int(raw["format_version"]) != BACKUP_FORMAT_VERSION:
            raise ValidationError("unsupported or malformed backup manifest")
        actual_hash = _sha256(backup)
        if actual_hash != raw["sha256"]:
            raise ValidationError("backup SHA-256 does not match manifest")

        source = sqlite3.connect(backup)
        try:
            source.execute("PRAGMA query_only = ON")
            integrity = _integrity_check(source)
            if integrity != "ok":
                raise ValidationError(f"backup integrity check failed: {integrity}")
            backup_schema = _schema_version(source)
            if backup_schema != int(raw["schema_version"]):
                raise ValidationError("backup schema version does not match manifest")
            if raw["integrity_check"] != "ok":
                raise ValidationError("backup manifest does not record a successful integrity check")
            account_count = int(source.execute("SELECT count(*) FROM accounts").fetchone()[0])
            if account_count != int(raw["account_count"]):
                raise ValidationError("backup account count does not match manifest")
            if backup_schema > SCHEMA_VERSION:
                raise ValidationError("backup schema is newer than this application")

            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = _temp_file(destination.parent, prefix=f".{destination.name}.restore-")
            try:
                target = sqlite3.connect(temp)
                try:
                    source.backup(target)
                    target.commit()
                finally:
                    target.close()
                with SQLiteCanonicalRepository(temp) as repo:
                    repo.initialize()
                    restored_schema = repo.schema_version()
                    integrity = _integrity_check(repo.connection)
                    foreign_key_violations = len(repo.connection.execute("PRAGMA foreign_key_check").fetchall())
                if integrity != "ok" or foreign_key_violations:
                    raise RuntimeError(
                        f"restored database validation failed: integrity={integrity}, "
                        f"foreign_key_violations={foreign_key_violations}"
                    )
                _private(temp)
                os.replace(temp, destination)
                _private(destination)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
        finally:
            source.close()

        return RestoreResult(
            destination=str(destination),
            backup_schema_version=backup_schema,
            restored_schema_version=restored_schema,
            sha256_verified=True,
            integrity_check=integrity,
            foreign_key_violations=foreign_key_violations,
        )

    def deletion_policy(self, policy: AccountDeletionPolicy | None = None) -> dict[str, Any]:
        policy = policy or AccountDeletionPolicy()
        return {
            **policy.to_dict(),
            "immediate_purge": "all account-scoped SQLite operational/private state",
            "retained_tombstone_fields": [
                "account_id",
                "deletion_id",
                "deleted_at",
                "purge_after",
                "policy_version",
                "retained_reason",
            ],
            "retained_audit_or_provenance": False,
            "raw_source_store": "NONE_CONFIGURED",
            "secret_store": "NONE_CONFIGURED",
            "secret_revocation": "NOT_APPLICABLE_NO_SECRET_STORE",
            "login_credentials": "PURGED_IMMEDIATELY_ALL_SESSIONS_INVALIDATED",
        }

    def delete_account(
        self,
        account_id: str,
        *,
        expected_server_revision: int,
        confirm_account_id: str,
        policy: AccountDeletionPolicy | None = None,
    ) -> AccountDeletionResult:
        if confirm_account_id != account_id:
            raise ValidationError("confirm_account_id must exactly match the server-bound account id")
        if expected_server_revision < 0:
            raise ValidationError("expected_server_revision must be >= 0")
        policy = policy or AccountDeletionPolicy()
        if not self.database.exists():
            raise ValidationError("source database does not exist")

        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("deletion clock must be timezone-aware")
        purge_after = now + timedelta(days=policy.tombstone_retention_days)
        deletion_id = str(uuid4())

        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            _assert_lifecycle_schema_known(connection)
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT server_revision FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if account is None:
                raise EntityNotFound("account not found")
            if int(account["server_revision"]) != expected_server_revision:
                raise VersionConflict("account server revision changed")

            deleted_rows: dict[str, int] = {"accounts": 1}
            for table in _DIRECT_ACCOUNT_TABLES:
                deleted_rows[table] = int(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE account_id=?", (account_id,)
                    ).fetchone()[0]
                )
            for table, query in _CHILD_TABLE_QUERIES.items():
                deleted_rows[table] = len(_rows(connection, query, (account_id,)))
            for table in _ACCOUNT_CREDENTIAL_TABLES:
                deleted_rows[table] = int(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE account_id=?", (account_id,)
                    ).fetchone()[0]
                )
                connection.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))

            connection.execute(
                "INSERT INTO account_deletion_tombstones("
                "account_id,deletion_id,deleted_at,purge_after,policy_version,retained_reason"
                ") VALUES (?,?,?,?,?,?)",
                (
                    account_id,
                    deletion_id,
                    _iso(now),
                    _iso(purge_after),
                    policy.policy_version,
                    policy.retained_reason,
                ),
            )
            cur = connection.execute(
                "DELETE FROM accounts WHERE id=? AND server_revision=?",
                (account_id, expected_server_revision),
            )
            if cur.rowcount != 1:
                raise VersionConflict("account server revision changed")

            for table in _DIRECT_ACCOUNT_TABLES:
                remaining = int(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE account_id=?", (account_id,)
                    ).fetchone()[0]
                )
                if remaining:
                    raise RuntimeError(f"account deletion left rows in {table}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"account deletion left foreign-key violations: {len(violations)}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return AccountDeletionResult(
            deletion_id=deletion_id,
            account_id=account_id,
            deleted_at=_iso(now),
            purge_after=_iso(purge_after),
            policy_version=policy.policy_version,
            retained_tombstone_fields=(
                "account_id",
                "deletion_id",
                "deleted_at",
                "purge_after",
                "policy_version",
                "retained_reason",
            ),
            deleted_rows=deleted_rows,
            secret_revocation_status="NOT_APPLICABLE_NO_SECRET_STORE",
        )

    def purge_expired_deletion_tombstones(self, *, now: datetime | None = None) -> int:
        when = now or self._now()
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValidationError("tombstone purge clock must be timezone-aware")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cur = connection.execute(
                "DELETE FROM account_deletion_tombstones WHERE purge_after<=?", (_iso(when),)
            )
            connection.commit()
            return int(cur.rowcount)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def export_account(self, account_id: str) -> AccountExport:
        if not self.database.exists():
            raise ValidationError("source database does not exist")
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            _assert_lifecycle_schema_known(connection)
            account = connection.execute("SELECT id,server_revision FROM accounts WHERE id=?", (account_id,)).fetchone()
            if account is None:
                raise EntityNotFound("account not found")
            tables: dict[str, list[dict[str, Any]]] = {
                "accounts": [dict(account)],
            }
            for table in _DIRECT_ACCOUNT_TABLES:
                tables[table] = _rows(
                    connection,
                    f"SELECT * FROM {table} WHERE account_id=? ORDER BY rowid",
                    (account_id,),
                )
            for table, query in _CHILD_TABLE_QUERIES.items():
                tables[table] = _rows(connection, query, (account_id,))
            return AccountExport(
                format_version=ACCOUNT_EXPORT_FORMAT_VERSION,
                application_version=__version__,
                schema_version=_schema_version(connection),
                exported_at=_iso(self._now()),
                account_id=account_id,
                contract={
                    "scope": "one account only",
                    "includes_private_locations": True,
                    "includes_provenance_and_workflow": True,
                    "includes_database_global_metadata": False,
                    "includes_connector_or_oauth_secrets": False,
                    "includes_login_credentials_or_sessions": False,
                    "note": (
                        "This release has no connector/OAuth secret store. The explicit table allowlist fails closed: "
                        "future secret tables are not exported unless the contract is deliberately revised."
                    ),
                },
                tables=tables,
            )
        finally:
            connection.close()

    def write_account_export(self, account_id: str, destination: str | Path) -> AccountExport:
        destination = Path(destination)
        if destination.exists():
            raise ValidationError("account export destination already exists")
        export = self.export_account(account_id)
        _atomic_text(destination, json.dumps(export.to_dict(), sort_keys=True, indent=2) + "\n")
        return export
