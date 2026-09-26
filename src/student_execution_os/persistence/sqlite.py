from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from student_execution_os.domain.clock import Clock, SystemClock
from student_execution_os.domain.errors import (
    DependencyCycleError,
    DuplicateHardCutoffOwner,
    EntityNotFound,
    UnsupportedCapability,
    ValidationError,
    VersionConflict,
)
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    CutoffBoundary,
    CutoffState,
    Dependency,
    DependencySuccessorKind,
    Event,
    EventLocationOption,
    EventTimeSemantics,
    HalfOpenInterval,
    HardCutoff,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    Milestone,
    MilestoneOwnerKind,
    MilestoneRole,
    MilestoneStatus,
    Obligation,
    ObligationCategory,
    ObligationKind,
    Project,
    ProjectStatus,
    Task,
    TemporalPrecision,
    UserTimeConstraint,
    UserTimeConstraintType,
    require_aware,
)

SCHEMA_VERSION = 16
_UNSET = object()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class SQLiteCanonicalRepository:
    """SQLite adapter for canonical local state.

    Task/Event subtype rows are mutable only through their parent Obligation version.
    Every planning-relevant commit advances the owning account's server_revision and
    appends one audit row in the same transaction.
    """

    def __init__(self, database: str | Path = ":memory:", *, clock: Clock | None = None) -> None:
        self.database = str(database)
        self.clock = clock or SystemClock()
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self._tx_depth = 0
        self._restrict_database_file_permissions()

    def _restrict_database_file_permissions(self) -> None:
        if os.name != "posix" or self.database == ":memory:" or self.database.startswith("file:"):
            return
        path = Path(self.database)
        if path.exists():
            path.chmod(0o600)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteCanonicalRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize(self) -> None:
        if self.database != ":memory:" and not self.database.startswith("file:"):
            # API and notification worker share the file; WAL lets readers proceed
            # while the other process writes.
            self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row[0]
            for row in self.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        }
        migrations = [
            (1, Path(__file__).with_name("migrations") / "001_initial.sql"),
            (2, Path(__file__).with_name("migrations") / "002_planning_projection.sql"),
            (3, Path(__file__).with_name("migrations") / "003_evidence_reconciliation.sql"),
            (4, Path(__file__).with_name("migrations") / "004_connector_sync.sql"),
            (5, Path(__file__).with_name("migrations") / "005_llm_action_boundary.sql"),
            (6, Path(__file__).with_name("migrations") / "006_travel_planning.sql"),
            (7, Path(__file__).with_name("migrations") / "007_recurrence_notifications.sql"),
            (8, Path(__file__).with_name("migrations") / "008_account_deletion.sql"),
            (9, Path(__file__).with_name("migrations") / "009_notification_delivery_outbox.sql"),
            (10, Path(__file__).with_name("migrations") / "010_auth.sql"),
            (11, Path(__file__).with_name("migrations") / "011_daily_product.sql"),
            (12, Path(__file__).with_name("migrations") / "012_execution_loop.sql"),
            (13, Path(__file__).with_name("migrations") / "013_capture_and_reminder_actions.sql"),
            (14, Path(__file__).with_name("migrations") / "014_llm_credentials.sql"),
            (15, Path(__file__).with_name("migrations") / "015_lifecycle_events_progress.sql"),
            (16, Path(__file__).with_name("migrations") / "016_reminders_and_health.sql"),
        ]
        for version, path in migrations:
            if version in applied:
                continue
            if version != (max(applied) + 1 if applied else 1):
                raise RuntimeError(f"non-contiguous migration sequence at version {version}")
            script = path.read_text(encoding="utf-8")
            self.connection.executescript(script)
            self.connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _iso(self.clock.now())),
            )
            self.connection.commit()
            applied.add(version)

    def schema_version(self) -> int:
        row = self.connection.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; nested use becomes a savepoint of the outer one.

        Nesting lets a caller (the sync command log) make a repository mutation and
        its own bookkeeping row commit together, while a failed inner mutation rolls
        back only its own writes.
        """
        if self._tx_depth:
            self._tx_depth += 1
            savepoint = f"sp_{self._tx_depth}"
            self.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute(f"ROLLBACK TO {savepoint}")
                self.connection.execute(f"RELEASE {savepoint}")
                raise
            else:
                self.connection.execute(f"RELEASE {savepoint}")
            finally:
                self._tx_depth -= 1
            return
        self._tx_depth = 1
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        finally:
            self._tx_depth = 0

    def create_account(self, account_id: str) -> None:
        if not account_id:
            raise ValidationError("account_id is required")
        now = self.clock.now()
        with self._tx() as conn:
            tombstone = conn.execute(
                "SELECT purge_after FROM account_deletion_tombstones WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if tombstone is not None:
                purge_after = _dt(str(tombstone["purge_after"]))
                if purge_after is None or purge_after > now:
                    raise ValidationError("account id is under deletion tombstone retention")
                conn.execute("DELETE FROM account_deletion_tombstones WHERE account_id=?", (account_id,))
            conn.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (account_id,))

    def get_server_revision(self, account_id: str) -> int:
        row = self.connection.execute("SELECT server_revision FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise EntityNotFound("account not found")
        return int(row[0])

    def list_audit(self, account_id: str) -> list[dict[str, object]]:
        self._require_account(account_id)
        rows = self.connection.execute(
            "SELECT server_revision, entity_type, entity_id, action, actor_category, committed_at, payload_json "
            "FROM audit_changes WHERE account_id=? ORDER BY server_revision",
            (account_id,),
        ).fetchall()
        return [
            {
                "server_revision": int(row["server_revision"]),
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "action": row["action"],
                "actor_category": row["actor_category"],
                "committed_at": row["committed_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def _require_account(self, account_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise EntityNotFound("account not found")

    def _record_change(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: str,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: ActorCategory,
        payload: dict[str, object] | None = None,
    ) -> int:
        cur = conn.execute(
            "UPDATE accounts SET server_revision=server_revision+1 WHERE id=?",
            (account_id,),
        )
        if cur.rowcount != 1:
            raise EntityNotFound("account not found")
        revision = int(
            conn.execute("SELECT server_revision FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO audit_changes(account_id, server_revision, entity_type, entity_id, action, actor_category, committed_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                revision,
                entity_type,
                entity_id,
                action,
                actor.value,
                _iso(self.clock.now()),
                json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
            ),
        )
        return revision

    def _new_obligation(
        self,
        *,
        account_id: str,
        kind: ObligationKind,
        category: ObligationCategory,
        title: str,
        description: str | None,
        importance: Importance,
        obligation_id: str | None,
        lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE,
    ) -> Obligation:
        now = self.clock.now()
        return Obligation(
            id=obligation_id or str(uuid4()),
            account_id=account_id,
            kind=kind,
            category=category,
            title=title,
            description=description,
            lifecycle_status=lifecycle_status,
            importance=importance,
            created_at=now,
            updated_at=now,
            completed_at=None,
            version=1,
        )

    def _insert_obligation(self, conn: sqlite3.Connection, obligation: Obligation) -> None:
        conn.execute(
            "INSERT INTO obligations(id,account_id,kind,category,title,description,lifecycle_status,importance,created_at,updated_at,completed_at,version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                obligation.id,
                obligation.account_id,
                obligation.kind.value,
                obligation.category.value,
                obligation.title,
                obligation.description,
                obligation.lifecycle_status.value,
                obligation.importance.value,
                _iso(obligation.created_at),
                _iso(obligation.updated_at),
                _iso(obligation.completed_at),
                obligation.version,
            ),
        )

    def create_task(
        self,
        *,
        account_id: str,
        title: str,
        category: ObligationCategory,
        importance: Importance,
        estimated_total_effort_minutes: int | None,
        remaining_effort_minutes: int | None,
        splittable: bool,
        actual_cutoff: HardCutoff,
        actor: ActorCategory,
        description: str | None = None,
        min_chunk_minutes: int | None = None,
        max_chunk_minutes: int | None = None,
        actionable_from: datetime | None = None,
        target_at: datetime | None = None,
        obligation_id: str | None = None,
        estimated_total_effort_low_minutes: int | None = None,
        estimated_total_effort_high_minutes: int | None = None,
        remaining_effort_low_minutes: int | None = None,
        remaining_effort_high_minutes: int | None = None,
        lifecycle_status: LifecycleStatus | None = None,
    ) -> Task:
        resolved_status = lifecycle_status or (
            LifecycleStatus.DRAFT if estimated_total_effort_minutes is None else LifecycleStatus.ACTIVE
        )
        obligation = self._new_obligation(
            account_id=account_id,
            kind=ObligationKind.TASK,
            category=category,
            title=title,
            description=description,
            importance=importance,
            obligation_id=obligation_id,
            lifecycle_status=resolved_status,
        )
        task = Task(
            obligation=obligation,
            estimated_total_effort_minutes=estimated_total_effort_minutes,
            remaining_effort_minutes=remaining_effort_minutes,
            splittable=splittable,
            min_chunk_minutes=min_chunk_minutes,
            max_chunk_minutes=max_chunk_minutes,
            actionable_from=actionable_from,
            actual_cutoff=actual_cutoff,
            target_at=target_at,
            estimated_total_effort_low_minutes=estimated_total_effort_low_minutes,
            estimated_total_effort_high_minutes=estimated_total_effort_high_minutes,
            remaining_effort_low_minutes=remaining_effort_low_minutes,
            remaining_effort_high_minutes=remaining_effort_high_minutes,
        )
        with self._tx() as conn:
            self._require_account(account_id)
            self._insert_obligation(conn, obligation)
            conn.execute(
                "INSERT INTO tasks(obligation_id,estimated_total_effort_minutes,remaining_effort_minutes,splittable,min_chunk_minutes,max_chunk_minutes,actionable_from,cutoff_state,actual_cutoff_at,cutoff_boundary,cutoff_precision,target_at,estimated_total_effort_low_minutes,estimated_total_effort_high_minutes,remaining_effort_low_minutes,remaining_effort_high_minutes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    obligation.id,
                    task.estimated_total_effort_minutes,
                    task.remaining_effort_minutes,
                    int(task.splittable),
                    task.min_chunk_minutes,
                    task.max_chunk_minutes,
                    _iso(task.actionable_from),
                    task.actual_cutoff.state.value,
                    _iso(task.actual_cutoff.at),
                    task.actual_cutoff.boundary.value if task.actual_cutoff.boundary else None,
                    task.actual_cutoff.precision.value if task.actual_cutoff.precision else None,
                    _iso(task.target_at),
                    task.estimated_total_effort_low_minutes,
                    task.estimated_total_effort_high_minutes,
                    task.remaining_effort_low_minutes,
                    task.remaining_effort_high_minutes,
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="OBLIGATION",
                entity_id=obligation.id,
                action="CREATE_TASK",
                actor=actor,
            )
        return task

    def get_task(self, account_id: str, obligation_id: str) -> Task:
        row = self.connection.execute(
            "SELECT o.*, t.estimated_total_effort_minutes,t.remaining_effort_minutes,t.splittable,t.min_chunk_minutes,t.max_chunk_minutes,t.actionable_from,t.cutoff_state,t.actual_cutoff_at,t.cutoff_boundary,t.cutoff_precision,t.target_at,t.estimated_total_effort_low_minutes,t.estimated_total_effort_high_minutes,t.remaining_effort_low_minutes,t.remaining_effort_high_minutes,t.started_at,t.last_progress_at "
            "FROM obligations o JOIN tasks t ON t.obligation_id=o.id WHERE o.account_id=? AND o.id=? AND o.kind='TASK'",
            (account_id, obligation_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("task not found")
        obligation = self._obligation_from_row(row)
        cutoff = HardCutoff(
            state=CutoffState(row["cutoff_state"]),
            at=_dt(row["actual_cutoff_at"]),
            boundary=CutoffBoundary(row["cutoff_boundary"]) if row["cutoff_boundary"] else None,
            precision=TemporalPrecision(row["cutoff_precision"]) if row["cutoff_precision"] else None,
        )
        return Task(
            obligation=obligation,
            estimated_total_effort_minutes=(
                None if row["estimated_total_effort_minutes"] is None else int(row["estimated_total_effort_minutes"])
            ),
            remaining_effort_minutes=(
                None if row["remaining_effort_minutes"] is None else int(row["remaining_effort_minutes"])
            ),
            splittable=bool(row["splittable"]),
            min_chunk_minutes=row["min_chunk_minutes"],
            max_chunk_minutes=row["max_chunk_minutes"],
            actionable_from=_dt(row["actionable_from"]),
            actual_cutoff=cutoff,
            target_at=_dt(row["target_at"]),
            estimated_total_effort_low_minutes=row["estimated_total_effort_low_minutes"],
            estimated_total_effort_high_minutes=row["estimated_total_effort_high_minutes"],
            remaining_effort_low_minutes=row["remaining_effort_low_minutes"],
            remaining_effort_high_minutes=row["remaining_effort_high_minutes"],
            started_at=_dt(row["started_at"]),
            last_progress_at=_dt(row["last_progress_at"]),
        )

    def _obligation_from_row(self, row: sqlite3.Row) -> Obligation:
        return Obligation(
            id=row["id"],
            account_id=row["account_id"],
            kind=ObligationKind(row["kind"]),
            category=ObligationCategory(row["category"]),
            title=row["title"],
            description=row["description"],
            lifecycle_status=LifecycleStatus(row["lifecycle_status"]),
            importance=Importance(row["importance"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            completed_at=_dt(row["completed_at"]),
            version=int(row["version"]),
        )

    def update_task(
        self,
        *,
        account_id: str,
        obligation_id: str,
        expected_version: int,
        actor: ActorCategory,
        title: str | object = _UNSET,
        description: str | None | object = _UNSET,
        importance: Importance | object = _UNSET,
        category: ObligationCategory | object = _UNSET,
        target_at: datetime | None | object = _UNSET,
        actionable_from: datetime | None | object = _UNSET,
        actual_cutoff: HardCutoff | object = _UNSET,
        remaining_effort_minutes: int | object = _UNSET,
        remaining_effort_low_minutes: int | None | object = _UNSET,
        remaining_effort_high_minutes: int | None | object = _UNSET,
        estimated_total_effort_minutes: int | None | object = _UNSET,
        splittable: bool | object = _UNSET,
        min_chunk_minutes: int | None | object = _UNSET,
        max_chunk_minutes: int | None | object = _UNSET,
        started_at: datetime | None | object = _UNSET,
        last_progress_at: datetime | None | object = _UNSET,
        activate: bool = False,
    ) -> Task:
        current = self.get_task(account_id, obligation_id)
        if actual_cutoff is not _UNSET and actual_cutoff != current.actual_cutoff:
            reconciled = self.connection.execute(
                "SELECT 1 FROM effective_fields WHERE account_id=? AND entity_ref=? AND field_path='actual_cutoff'",
                (account_id, obligation_id),
            ).fetchone()
            if reconciled is not None:
                raise ValidationError(
                    "actual_cutoff is reconciliation-owned for this task; use an explicit reconciliation override"
                )
        if current.obligation.version != expected_version:
            raise VersionConflict(
                f"expected obligation version {expected_version}, current {current.obligation.version}"
            )
        if title is not _UNSET:
            title = str(title).strip()
            if not title:
                raise ValidationError("title is required")
        now = self.clock.now()
        next_status = LifecycleStatus.ACTIVE if activate else current.obligation.lifecycle_status
        new_obligation = replace(
            current.obligation,
            lifecycle_status=next_status,
            updated_at=now,
            version=expected_version + 1,
            title=current.obligation.title if title is _UNSET else title,
            description=current.obligation.description if description is _UNSET else (description or None),
            importance=current.obligation.importance if importance is _UNSET else Importance(importance),
            category=current.obligation.category if category is _UNSET else ObligationCategory(category),
        )
        next_estimate = (
            current.estimated_total_effort_minutes
            if estimated_total_effort_minutes is _UNSET else estimated_total_effort_minutes
        )
        next_remaining = (
            current.remaining_effort_minutes
            if remaining_effort_minutes is _UNSET else remaining_effort_minutes
        )
        if current.estimated_total_effort_minutes is None and next_estimate is not None and remaining_effort_minutes is _UNSET:
            next_remaining = next_estimate
        candidate = Task(
            obligation=new_obligation,
            estimated_total_effort_minutes=next_estimate,
            remaining_effort_minutes=next_remaining,
            splittable=current.splittable if splittable is _UNSET else bool(splittable),
            min_chunk_minutes=current.min_chunk_minutes if min_chunk_minutes is _UNSET else min_chunk_minutes,
            max_chunk_minutes=current.max_chunk_minutes if max_chunk_minutes is _UNSET else max_chunk_minutes,
            actionable_from=current.actionable_from if actionable_from is _UNSET else actionable_from,
            actual_cutoff=current.actual_cutoff if actual_cutoff is _UNSET else actual_cutoff,
            target_at=current.target_at if target_at is _UNSET else target_at,
            estimated_total_effort_low_minutes=current.estimated_total_effort_low_minutes,
            estimated_total_effort_high_minutes=current.estimated_total_effort_high_minutes,
            remaining_effort_low_minutes=(current.remaining_effort_low_minutes if remaining_effort_low_minutes is _UNSET else remaining_effort_low_minutes),
            remaining_effort_high_minutes=(current.remaining_effort_high_minutes if remaining_effort_high_minutes is _UNSET else remaining_effort_high_minutes),
            started_at=current.started_at if started_at is _UNSET else started_at,
            last_progress_at=current.last_progress_at if last_progress_at is _UNSET else last_progress_at,
        )
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE obligations SET lifecycle_status=?,title=?,description=?,importance=?,category=?,updated_at=?, version=? "
                "WHERE account_id=? AND id=? AND version=?",
                (
                    next_status.value, new_obligation.title, new_obligation.description,
                    new_obligation.importance.value, new_obligation.category.value,
                    _iso(now), expected_version + 1, account_id, obligation_id, expected_version,
                ),
            )
            if cur.rowcount != 1:
                raise VersionConflict("obligation version changed before commit")
            conn.execute(
                "UPDATE tasks SET estimated_total_effort_minutes=?,remaining_effort_minutes=?,remaining_effort_low_minutes=?,remaining_effort_high_minutes=?,splittable=?,min_chunk_minutes=?,max_chunk_minutes=?,actionable_from=?,cutoff_state=?,actual_cutoff_at=?,cutoff_boundary=?,cutoff_precision=?,target_at=?,started_at=?,last_progress_at=? WHERE obligation_id=?",
                (
                    candidate.estimated_total_effort_minutes,
                    candidate.remaining_effort_minutes,
                    candidate.remaining_effort_low_minutes,
                    candidate.remaining_effort_high_minutes,
                    int(candidate.splittable),
                    candidate.min_chunk_minutes,
                    candidate.max_chunk_minutes,
                    _iso(candidate.actionable_from),
                    candidate.actual_cutoff.state.value,
                    _iso(candidate.actual_cutoff.at),
                    candidate.actual_cutoff.boundary.value if candidate.actual_cutoff.boundary else None,
                    candidate.actual_cutoff.precision.value if candidate.actual_cutoff.precision else None,
                    _iso(candidate.target_at),
                    _iso(candidate.started_at),
                    _iso(candidate.last_progress_at),
                    obligation_id,
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="OBLIGATION",
                entity_id=obligation_id,
                action="UPDATE_TASK",
                actor=actor,
            )
        return candidate

    def activate_task(
        self, *, account_id: str, obligation_id: str, expected_version: int, actor: ActorCategory
    ) -> Task:
        current = self.get_task(account_id, obligation_id)
        if current.obligation.lifecycle_status is not LifecycleStatus.DRAFT:
            raise ValidationError("only DRAFT tasks can be activated")
        return self.update_task(
            account_id=account_id,
            obligation_id=obligation_id,
            expected_version=expected_version,
            actor=actor,
            activate=True,
        )

    def get_obligation(self, account_id: str, obligation_id: str) -> Obligation:
        row = self.connection.execute(
            "SELECT * FROM obligations WHERE account_id=? AND id=?",
            (account_id, obligation_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("obligation not found")
        return self._obligation_from_row(row)

    def _transition_obligation_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: str,
        obligation_id: str,
        expected_version: int,
        actor: ActorCategory,
        action: str,
        audit_payload: dict[str, object] | None = None,
    ) -> Obligation:
        row = conn.execute(
            "SELECT * FROM obligations WHERE account_id=? AND id=?",
            (account_id, obligation_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("obligation not found")
        current = self._obligation_from_row(row)
        if current.version != expected_version:
            raise VersionConflict(
                f"expected version {expected_version}, current {current.version}"
            )
        now = self.clock.now()
        if action == "COMPLETE":
            candidate = current.completed(now)
        elif action == "CANCEL":
            candidate = current.cancelled(now)
        elif action == "ARCHIVE":
            candidate = current.archived(now)
        elif action == "UNARCHIVE":
            candidate = current.unarchived(now)
        elif action == "REOPEN":
            candidate = current.reopened(now)
            if current.kind is ObligationKind.TASK:
                task_row = conn.execute(
                    "SELECT estimated_total_effort_minutes FROM tasks WHERE obligation_id=?",
                    (obligation_id,),
                ).fetchone()
                if task_row is not None and task_row["estimated_total_effort_minutes"] is None:
                    # Reopening must not manufacture an ACTIVE task that violates
                    # the positive-effort invariant. An unrefined task returns to
                    # the refinement queue and can be activated explicitly later.
                    candidate = replace(candidate, lifecycle_status=LifecycleStatus.DRAFT)
        else:
            raise AssertionError(action)
        cur = conn.execute(
            "UPDATE obligations SET lifecycle_status=?,completed_at=?,updated_at=?,version=? "
            "WHERE account_id=? AND id=? AND version=?",
            (
                candidate.lifecycle_status.value,
                _iso(candidate.completed_at),
                _iso(candidate.updated_at),
                candidate.version,
                account_id,
                obligation_id,
                expected_version,
            ),
        )
        if cur.rowcount != 1:
            raise VersionConflict("obligation version changed before commit")
        self._record_change(
            conn,
            account_id=account_id,
            entity_type="OBLIGATION",
            entity_id=obligation_id,
            action=action,
            actor=actor,
            payload=audit_payload,
        )
        if action in ("COMPLETE", "CANCEL"):
            # A finished or cancelled obligation has nothing left to remind about:
            # undelivered reminders stop immediately and the reminder episode closes.
            conn.execute(
                "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                "last_error=? WHERE account_id=? AND delivery_state IN ('PENDING','LEASED') AND EXISTS ("
                "SELECT 1 FROM json_each(reminder_messages.task_ids_json) WHERE value=?)",
                ("TASK_COMPLETED" if action == "COMPLETE" else "TASK_CANCELLED", account_id, obligation_id),
            )
            conn.execute(
                "UPDATE reminder_states SET closed_reason=?,next_check_at=NULL,updated_at=? WHERE account_id=? AND task_id=?",
                ("COMPLETED" if action == "COMPLETE" else "CANCELLED", _iso(now), account_id, obligation_id),
            )
        return candidate

    def _transition_obligation(
        self,
        *,
        account_id: str,
        obligation_id: str,
        expected_version: int,
        actor: ActorCategory,
        action: str,
        audit_payload: dict[str, object] | None = None,
    ) -> Obligation:
        with self._tx() as conn:
            return self._transition_obligation_in_tx(
                conn,
                account_id=account_id,
                obligation_id=obligation_id,
                expected_version=expected_version,
                actor=actor,
                action=action,
                audit_payload=audit_payload,
            )

    def complete_obligation(self, **kwargs) -> Obligation:
        return self._transition_obligation(action="COMPLETE", **kwargs)

    def cancel_obligation(self, **kwargs) -> Obligation:
        return self._transition_obligation(action="CANCEL", **kwargs)

    def reopen_obligation(self, **kwargs) -> Obligation:
        return self._transition_obligation(action="REOPEN", **kwargs)

    def archive_obligation(self, **kwargs) -> Obligation:
        return self._transition_obligation(action="ARCHIVE", **kwargs)

    def unarchive_obligation(self, **kwargs) -> Obligation:
        return self._transition_obligation(action="UNARCHIVE", **kwargs)

    def is_deleted_obligation(self, account_id: str, obligation_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deleted_obligations WHERE account_id=? AND obligation_id=?", (account_id, obligation_id)
        ).fetchone() is not None

    def delete_obligation(
        self, *, account_id: str, obligation_id: str, expected_version: int, actor: ActorCategory
    ) -> None:
        """Remove a user's task or event for good.

        Every row that references it cascades (task/event subtype, evidence links,
        overrides, attachments links, pins); reminder state and undelivered reminder
        messages are removed explicitly because they reference it by value. A tombstone
        keeps late offline replays from resurrecting it.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT kind,version FROM obligations WHERE account_id=? AND id=?", (account_id, obligation_id)
            ).fetchone()
            if row is None:
                raise EntityNotFound("obligation not found")
            if int(row["version"]) != expected_version:
                raise VersionConflict(f"expected version {expected_version}, current {row['version']}")
            now = self.clock.now()
            conn.execute(
                "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                "last_error='DELETED' WHERE account_id=? AND delivery_state IN ('PENDING','LEASED') AND EXISTS ("
                "SELECT 1 FROM json_each(reminder_messages.task_ids_json) WHERE value=?)",
                (account_id, obligation_id),
            )
            conn.execute("DELETE FROM reminder_states WHERE account_id=? AND task_id=?", (account_id, obligation_id))
            conn.execute(
                "DELETE FROM attachment_links WHERE account_id=? AND owner_kind='OBLIGATION' AND owner_id=?",
                (account_id, obligation_id),
            )
            conn.execute(
                "DELETE FROM dependencies WHERE account_id=? AND (predecessor_task_id=? OR successor_id=?)",
                (account_id, obligation_id, obligation_id),
            )
            conn.execute(
                "DELETE FROM milestones WHERE account_id=? AND owner_kind='OBLIGATION' AND owner_id=?",
                (account_id, obligation_id),
            )
            conn.execute("DELETE FROM obligations WHERE account_id=? AND id=?", (account_id, obligation_id))
            conn.execute(
                "INSERT OR REPLACE INTO deleted_obligations(account_id,obligation_id,kind,deleted_at) VALUES (?,?,?,?)",
                (account_id, obligation_id, row["kind"], _iso(now)),
            )
            self._record_change(
                conn, account_id=account_id, entity_type="OBLIGATION", entity_id=obligation_id,
                action="DELETE", actor=actor,
            )

    def _validate_location_effect_places(
        self,
        *,
        account_id: str,
        location_effect: LocationEffect,
    ) -> None:
        place_ids: tuple[str, ...]
        if location_effect.kind is LocationEffectKind.STAY:
            assert location_effect.destination_place_id is not None
            place_ids = (location_effect.destination_place_id,)
        elif location_effect.kind is LocationEffectKind.MOVE:
            assert location_effect.origin_place_id is not None
            assert location_effect.destination_place_id is not None
            place_ids = (
                location_effect.origin_place_id,
                location_effect.destination_place_id,
            )
        else:
            return
        for place_id in place_ids:
            row = self.connection.execute(
                "SELECT 1 FROM places WHERE account_id=? AND id=?",
                (account_id, place_id),
            ).fetchone()
            if row is None:
                raise EntityNotFound("location effect place not found")

    def create_event(
        self,
        *,
        account_id: str,
        title: str,
        time_semantics: EventTimeSemantics,
        actor: ActorCategory,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        category: ObligationCategory = ObligationCategory.GENERAL,
        importance: Importance = Importance.NORMAL,
        attendance_policy: AttendancePolicy = AttendancePolicy.REQUIRED,
        location_effect: LocationEffect | None = None,
        arrival_requirement_minutes: int = 0,
        description: str | None = None,
        obligation_id: str | None = None,
        location_options: tuple[EventLocationOption, ...] = (),
        selected_location_option_id: str | None = None,
    ) -> Event:
        if time_semantics is not EventTimeSemantics.FIXED_INTERVAL:
            raise UnsupportedCapability("FLEXIBLE_WINDOW is not enabled in this release")
        if starts_at is None or ends_at is None:
            raise ValidationError("fixed Event requires starts_at and ends_at")
        return self.create_fixed_event(
            account_id=account_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            actor=actor,
            category=category,
            importance=importance,
            attendance_policy=attendance_policy,
            location_effect=location_effect,
            arrival_requirement_minutes=arrival_requirement_minutes,
            description=description,
            obligation_id=obligation_id,
            location_options=location_options,
            selected_location_option_id=selected_location_option_id,
        )

    def create_fixed_event(
        self,
        *,
        account_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        actor: ActorCategory,
        category: ObligationCategory = ObligationCategory.GENERAL,
        importance: Importance = Importance.NORMAL,
        attendance_policy: AttendancePolicy = AttendancePolicy.REQUIRED,
        location_effect: LocationEffect | None = None,
        arrival_requirement_minutes: int = 0,
        description: str | None = None,
        obligation_id: str | None = None,
        location_options: tuple[EventLocationOption, ...] = (),
        selected_location_option_id: str | None = None,
    ) -> Event:
        self._require_account(account_id)
        if selected_location_option_id is not None:
            selected = next((item for item in location_options if item.id == selected_location_option_id), None)
            if selected is None:
                raise ValidationError("selected location option does not exist")
            resolved_location_effect = selected.effect
        else:
            resolved_location_effect = location_effect or LocationEffect()
        for option in location_options:
            self._validate_location_effect_places(account_id=account_id, location_effect=option.effect)
        self._validate_location_effect_places(
            account_id=account_id,
            location_effect=resolved_location_effect,
        )
        obligation = self._new_obligation(
            account_id=account_id,
            kind=ObligationKind.EVENT,
            category=category,
            title=title,
            description=description,
            importance=importance,
            obligation_id=obligation_id,
        )
        event = Event(
            obligation=obligation,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            interval=HalfOpenInterval(starts_at, ends_at),
            attendance_policy=attendance_policy,
            location_effect=resolved_location_effect,
            arrival_requirement_minutes=arrival_requirement_minutes,
            location_options=location_options,
            selected_location_option_id=selected_location_option_id,
        )
        with self._tx() as conn:
            self._require_account(account_id)
            self._insert_obligation(conn, obligation)
            conn.execute(
                "INSERT INTO events(obligation_id,time_semantics,starts_at,ends_at,attendance_policy,location_effect_kind,origin_place_id,destination_place_id,arrival_requirement_minutes,selected_location_option_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    obligation.id,
                    event.time_semantics.value,
                    _iso(event.interval.starts_at),
                    _iso(event.interval.ends_at),
                    event.attendance_policy.value,
                    event.location_effect.kind.value,
                    event.location_effect.origin_place_id,
                    event.location_effect.destination_place_id,
                    event.arrival_requirement_minutes,
                    event.selected_location_option_id,
                ),
            )
            for option in event.location_options:
                conn.execute(
                    "INSERT INTO event_location_options(id,account_id,event_id,label,location_effect_kind,origin_place_id,destination_place_id) VALUES (?,?,?,?,?,?,?)",
                    (option.id, account_id, obligation.id, option.label, option.effect.kind.value,
                     option.effect.origin_place_id, option.effect.destination_place_id),
                )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="OBLIGATION",
                entity_id=obligation.id,
                action="CREATE_EVENT",
                actor=actor,
            )
        return event

    def update_fixed_event(
        self,
        *,
        account_id: str,
        obligation_id: str,
        expected_version: int,
        starts_at: datetime,
        ends_at: datetime,
        actor: ActorCategory,
        attendance_policy: AttendancePolicy | None = None,
        title: str | object = _UNSET,
        description: str | None | object = _UNSET,
        category: ObligationCategory | object = _UNSET,
        importance: Importance | object = _UNSET,
    ) -> Event:
        current = self.get_event(account_id, obligation_id)
        if current.obligation.version != expected_version:
            raise VersionConflict(
                f"expected obligation version {expected_version}, current {current.obligation.version}"
            )
        interval = HalfOpenInterval(starts_at, ends_at)
        policy = attendance_policy or current.attendance_policy
        ob = current.obligation
        next_title = ob.title if title is _UNSET else str(title).strip()
        if not next_title:
            raise ValidationError("title is required")
        next_description = ob.description if description is _UNSET else (description or None)
        next_category = ob.category if category is _UNSET else ObligationCategory(category)
        next_importance = ob.importance if importance is _UNSET else Importance(importance)
        now = self.clock.now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE obligations SET title=?,description=?,category=?,importance=?,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=? AND version=?",
                (next_title, next_description, next_category.value, next_importance.value, _iso(now),
                 account_id, obligation_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("obligation version changed before commit")
            conn.execute(
                "UPDATE events SET starts_at=?,ends_at=?,attendance_policy=? WHERE obligation_id=?",
                (_iso(interval.starts_at), _iso(interval.ends_at), policy.value, obligation_id),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="OBLIGATION",
                entity_id=obligation_id,
                action="UPDATE_EVENT",
                actor=actor,
            )
        return self.get_event(account_id, obligation_id)

    def get_event(self, account_id: str, obligation_id: str) -> Event:
        row = self.connection.execute(
            "SELECT o.*,e.time_semantics,e.starts_at,e.ends_at,e.attendance_policy,e.location_effect_kind,e.origin_place_id,e.destination_place_id,e.arrival_requirement_minutes,e.selected_location_option_id "
            "FROM obligations o JOIN events e ON e.obligation_id=o.id WHERE o.account_id=? AND o.id=? AND o.kind='EVENT'",
            (account_id, obligation_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("event not found")
        option_rows = self.connection.execute(
            "SELECT * FROM event_location_options WHERE account_id=? AND event_id=? ORDER BY id",
            (account_id, obligation_id),
        ).fetchall()
        options = tuple(EventLocationOption(
            id=item["id"], label=item["label"],
            effect=LocationEffect(LocationEffectKind(item["location_effect_kind"]), item["origin_place_id"], item["destination_place_id"]),
        ) for item in option_rows)
        return Event(
            obligation=self._obligation_from_row(row),
            time_semantics=EventTimeSemantics(row["time_semantics"]),
            interval=HalfOpenInterval(_dt(row["starts_at"]), _dt(row["ends_at"])),
            attendance_policy=AttendancePolicy(row["attendance_policy"]),
            location_effect=LocationEffect(
                LocationEffectKind(row["location_effect_kind"]),
                row["origin_place_id"],
                row["destination_place_id"],
            ),
            arrival_requirement_minutes=int(row["arrival_requirement_minutes"]),
            location_options=options,
            selected_location_option_id=row["selected_location_option_id"],
        )

    def select_event_location_option(
        self, *, account_id: str, obligation_id: str, option_id: str,
        expected_version: int, actor: ActorCategory,
    ) -> Event:
        current = self.get_event(account_id, obligation_id)
        if current.obligation.version != expected_version:
            raise VersionConflict("event version changed")
        selected = next((option for option in current.location_options if option.id == option_id), None)
        if selected is None:
            raise EntityNotFound("event location option not found")
        now = self.clock.now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE obligations SET updated_at=?,version=version+1 WHERE account_id=? AND id=? AND version=?",
                (_iso(now), account_id, obligation_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("event version changed before location selection")
            conn.execute(
                "UPDATE events SET selected_location_option_id=?,location_effect_kind=?,origin_place_id=?,destination_place_id=? WHERE obligation_id=?",
                (option_id, selected.effect.kind.value, selected.effect.origin_place_id,
                 selected.effect.destination_place_id, obligation_id),
            )
            self._record_change(conn, account_id=account_id, entity_type="OBLIGATION", entity_id=obligation_id,
                                action="SELECT_EVENT_LOCATION_OPTION", actor=actor, payload={"option_id": option_id})
        return self.get_event(account_id, obligation_id)

    def create_project(
        self,
        *,
        account_id: str,
        title: str,
        actor: ActorCategory,
        description: str | None = None,
        importance: Importance | None = None,
        project_id: str | None = None,
    ) -> Project:
        now = self.clock.now()
        project = Project(
            id=project_id or str(uuid4()),
            account_id=account_id,
            title=title,
            description=description,
            status=ProjectStatus.ACTIVE,
            importance=importance,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._tx() as conn:
            self._require_account(account_id)
            conn.execute(
                "INSERT INTO projects(id,account_id,title,description,status,importance,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    project.id,
                    project.account_id,
                    project.title,
                    project.description,
                    project.status.value,
                    project.importance.value if project.importance else None,
                    project.version,
                    _iso(project.created_at),
                    _iso(project.updated_at),
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="PROJECT",
                entity_id=project.id,
                action="CREATE_PROJECT",
                actor=actor,
            )
        return project

    def get_project(self, account_id: str, project_id: str) -> Project:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE account_id=? AND id=?", (account_id, project_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("project not found")
        return Project(
            id=row["id"],
            account_id=row["account_id"],
            title=row["title"],
            description=row["description"],
            status=ProjectStatus(row["status"]),
            importance=Importance(row["importance"]) if row["importance"] else None,
            version=int(row["version"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def _transition_project(
        self,
        *,
        account_id: str,
        project_id: str,
        expected_version: int,
        actor: ActorCategory,
        action: str,
    ) -> Project:
        project = self.get_project(account_id, project_id)
        if project.version != expected_version:
            raise VersionConflict(f"expected project version {expected_version}, current {project.version}")
        if project.status is ProjectStatus.ARCHIVED:
            raise ValidationError("archived project cannot transition")
        if action == "COMPLETE_PROJECT":
            new_status = ProjectStatus.COMPLETED
        elif action == "CANCEL_PROJECT":
            new_status = ProjectStatus.CANCELLED
        elif action == "REOPEN_PROJECT":
            if project.status not in (ProjectStatus.COMPLETED, ProjectStatus.CANCELLED):
                raise ValidationError("only completed/cancelled projects can be reopened")
            new_status = ProjectStatus.ACTIVE
        else:
            raise AssertionError(action)
        now = self.clock.now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE projects SET status=?,version=version+1,updated_at=? WHERE account_id=? AND id=? AND version=?",
                (new_status.value, _iso(now), account_id, project_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("project version changed before commit")
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="PROJECT",
                entity_id=project_id,
                action=action,
                actor=actor,
            )
        return self.get_project(account_id, project_id)

    def complete_project(self, **kwargs) -> Project:
        return self._transition_project(action="COMPLETE_PROJECT", **kwargs)

    def cancel_project(self, **kwargs) -> Project:
        return self._transition_project(action="CANCEL_PROJECT", **kwargs)

    def reopen_project(self, **kwargs) -> Project:
        return self._transition_project(action="REOPEN_PROJECT", **kwargs)

    def add_project_member(
        self,
        *,
        account_id: str,
        project_id: str,
        obligation_id: str,
        expected_version: int,
        actor: ActorCategory,
    ) -> Project:
        project = self.get_project(account_id, project_id)
        if project.version != expected_version:
            raise VersionConflict(f"expected project version {expected_version}, current {project.version}")
        if self.connection.execute(
            "SELECT 1 FROM obligations WHERE account_id=? AND id=?", (account_id, obligation_id)
        ).fetchone() is None:
            raise EntityNotFound("obligation not found")
        now = self.clock.now()
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO project_members(account_id,project_id,obligation_id) VALUES (?,?,?)",
                (account_id, project_id, obligation_id),
            )
            cur = conn.execute(
                "UPDATE projects SET version=version+1,updated_at=? WHERE account_id=? AND id=? AND version=?",
                (_iso(now), account_id, project_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("project version changed before commit")
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="PROJECT",
                entity_id=project_id,
                action="ADD_MEMBER",
                actor=actor,
                payload={"obligation_id": obligation_id},
            )
        return self.get_project(account_id, project_id)

    def create_milestone(
        self,
        *,
        account_id: str,
        owner_kind: MilestoneOwnerKind,
        owner_id: str,
        title: str,
        marker_at: datetime,
        role: MilestoneRole,
        actor: ActorCategory,
        consequence: str | None = None,
        hard_for_planning: bool = False,
        milestone_id: str | None = None,
    ) -> Milestone:
        require_aware(marker_at, "marker_at")
        if owner_kind is MilestoneOwnerKind.OBLIGATION and role is MilestoneRole.FINAL_CUTOFF:
            raise DuplicateHardCutoffOwner(
                "Obligation.actual_cutoff is the canonical owner; an Obligation milestone cannot independently own FINAL_CUTOFF"
            )
        if owner_kind is MilestoneOwnerKind.OBLIGATION:
            exists = self.connection.execute(
                "SELECT 1 FROM obligations WHERE account_id=? AND id=?", (account_id, owner_id)
            ).fetchone()
        else:
            exists = self.connection.execute(
                "SELECT 1 FROM projects WHERE account_id=? AND id=?", (account_id, owner_id)
            ).fetchone()
        if exists is None:
            raise EntityNotFound("milestone owner not found")
        milestone = Milestone(
            id=milestone_id or str(uuid4()),
            account_id=account_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            title=title,
            marker_at=marker_at,
            role=role,
            consequence=consequence,
            hard_for_planning=hard_for_planning,
            status=MilestoneStatus.ACTIVE,
            version=1,
        )
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO milestones(id,account_id,owner_kind,owner_id,title,marker_at,role,consequence,hard_for_planning,status,version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    milestone.id,
                    milestone.account_id,
                    milestone.owner_kind.value,
                    milestone.owner_id,
                    milestone.title,
                    _iso(milestone.marker_at),
                    milestone.role.value,
                    milestone.consequence,
                    int(milestone.hard_for_planning),
                    milestone.status.value,
                    milestone.version,
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="MILESTONE",
                entity_id=milestone.id,
                action="CREATE_MILESTONE",
                actor=actor,
            )
        return milestone

    def update_milestone(
        self,
        *,
        account_id: str,
        milestone_id: str,
        expected_version: int,
        actor: ActorCategory,
        marker_at: datetime | None = None,
        title: str | None = None,
    ) -> Milestone:
        row = self.connection.execute(
            "SELECT * FROM milestones WHERE account_id=? AND id=?", (account_id, milestone_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("milestone not found")
        current_version = int(row["version"])
        if current_version != expected_version:
            raise VersionConflict(f"expected milestone version {expected_version}, current {current_version}")
        new_marker = marker_at or _dt(row["marker_at"])
        require_aware(new_marker, "marker_at")
        new_title = title if title is not None else row["title"]
        if not new_title.strip():
            raise ValidationError("milestone title cannot be empty")
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE milestones SET title=?,marker_at=?,version=version+1 WHERE account_id=? AND id=? AND version=?",
                (new_title, _iso(new_marker), account_id, milestone_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("milestone version changed before commit")
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="MILESTONE",
                entity_id=milestone_id,
                action="UPDATE_MILESTONE",
                actor=actor,
            )
        updated = self.connection.execute(
            "SELECT * FROM milestones WHERE account_id=? AND id=?", (account_id, milestone_id)
        ).fetchone()
        return Milestone(
            id=updated["id"], account_id=updated["account_id"],
            owner_kind=MilestoneOwnerKind(updated["owner_kind"]), owner_id=updated["owner_id"],
            title=updated["title"], marker_at=_dt(updated["marker_at"]),
            role=MilestoneRole(updated["role"]), consequence=updated["consequence"],
            hard_for_planning=bool(updated["hard_for_planning"]),
            status=MilestoneStatus(updated["status"]), version=int(updated["version"]),
        )

    def add_dependency(
        self,
        *,
        account_id: str,
        predecessor_task_id: str,
        successor_kind: DependencySuccessorKind,
        successor_id: str,
        actor: ActorCategory,
        dependency_id: str | None = None,
    ) -> Dependency:
        self.get_task(account_id, predecessor_task_id)
        if successor_kind is DependencySuccessorKind.TASK:
            self.get_task(account_id, successor_id)
        elif successor_kind is DependencySuccessorKind.EVENT:
            self.get_event(account_id, successor_id)
        else:
            if self.connection.execute(
                "SELECT 1 FROM milestones WHERE account_id=? AND id=?", (account_id, successor_id)
            ).fetchone() is None:
                raise EntityNotFound("successor milestone not found")
        dependency = Dependency(
            id=dependency_id or str(uuid4()),
            account_id=account_id,
            predecessor_task_id=predecessor_task_id,
            successor_kind=successor_kind,
            successor_id=successor_id,
        )
        if successor_kind is DependencySuccessorKind.TASK:
            self._reject_task_cycle(account_id, predecessor_task_id, successor_id)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO dependencies(id,account_id,predecessor_task_id,successor_kind,successor_id) VALUES (?,?,?,?,?)",
                (
                    dependency.id,
                    account_id,
                    predecessor_task_id,
                    successor_kind.value,
                    successor_id,
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="DEPENDENCY",
                entity_id=dependency.id,
                action="CREATE_DEPENDENCY",
                actor=actor,
            )
        return dependency

    def _reject_task_cycle(self, account_id: str, predecessor: str, successor: str) -> None:
        if predecessor == successor:
            raise DependencyCycleError("task cannot depend on itself")
        rows = self.connection.execute(
            "SELECT predecessor_task_id,successor_id FROM dependencies "
            "WHERE account_id=? AND successor_kind='TASK'",
            (account_id,),
        ).fetchall()
        graph: dict[str, set[str]] = {}
        for row in rows:
            graph.setdefault(row["predecessor_task_id"], set()).add(row["successor_id"])
        graph.setdefault(predecessor, set()).add(successor)
        stack = [successor]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == predecessor:
                raise DependencyCycleError("hard task dependency would create a cycle")
            if node in seen:
                continue
            seen.add(node)
            stack.extend(graph.get(node, ()))

    def create_time_constraint(
        self,
        *,
        account_id: str,
        type: UserTimeConstraintType,
        starts_at: datetime,
        ends_at: datetime,
        actor: ActorCategory,
        obligation_id: str | None = None,
        reason: str | None = None,
        constraint_id: str | None = None,
    ) -> UserTimeConstraint:
        interval = HalfOpenInterval(starts_at, ends_at)
        obligation_row = None
        if obligation_id is not None:
            obligation_row = self.connection.execute(
                "SELECT kind FROM obligations WHERE account_id=? AND id=?", (account_id, obligation_id)
            ).fetchone()
            if obligation_row is None:
                raise EntityNotFound("obligation not found")
        if type is UserTimeConstraintType.PINNED_WORK and (
            obligation_row is None or obligation_row["kind"] != ObligationKind.TASK.value
        ):
            raise ValidationError("PINNED_WORK must reference a Task obligation")
        constraint = UserTimeConstraint(
            id=constraint_id or str(uuid4()),
            account_id=account_id,
            type=type,
            interval=interval,
            obligation_id=obligation_id,
            reason=reason,
            version=1,
        )
        with self._tx() as conn:
            self._require_account(account_id)
            conn.execute(
                "INSERT INTO user_time_constraints(id,account_id,type,starts_at,ends_at,obligation_id,reason,version) VALUES (?,?,?,?,?,?,?,?)",
                (
                    constraint.id,
                    account_id,
                    constraint.type.value,
                    _iso(interval.starts_at),
                    _iso(interval.ends_at),
                    constraint.obligation_id,
                    constraint.reason,
                    constraint.version,
                ),
            )
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="USER_TIME_CONSTRAINT",
                entity_id=constraint.id,
                action="CREATE_CONSTRAINT",
                actor=actor,
            )
        return constraint

    def get_time_constraint(self, account_id: str, constraint_id: str) -> UserTimeConstraint:
        row = self.connection.execute(
            "SELECT * FROM user_time_constraints WHERE account_id=? AND id=?", (account_id, constraint_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("time constraint not found")
        return UserTimeConstraint(
            id=row["id"],
            account_id=row["account_id"],
            type=UserTimeConstraintType(row["type"]),
            interval=HalfOpenInterval(_dt(row["starts_at"]), _dt(row["ends_at"])),
            obligation_id=row["obligation_id"],
            reason=row["reason"],
            version=int(row["version"]),
        )

    def update_time_constraint(
        self,
        *,
        account_id: str,
        constraint_id: str,
        expected_version: int,
        starts_at: datetime,
        ends_at: datetime,
        actor: ActorCategory,
        reason: str | None | object = _UNSET,
    ) -> UserTimeConstraint:
        current = self.get_time_constraint(account_id, constraint_id)
        if current.version != expected_version:
            raise VersionConflict(f"expected constraint version {expected_version}, current {current.version}")
        interval = HalfOpenInterval(starts_at, ends_at)
        new_reason = current.reason if reason is _UNSET else reason
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE user_time_constraints SET starts_at=?,ends_at=?,reason=?,version=version+1 "
                "WHERE account_id=? AND id=? AND version=?",
                (_iso(starts_at), _iso(ends_at), new_reason, account_id, constraint_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("constraint version changed before commit")
            self._record_change(
                conn,
                account_id=account_id,
                entity_type="USER_TIME_CONSTRAINT",
                entity_id=constraint_id,
                action="UPDATE_CONSTRAINT",
                actor=actor,
            )
        return self.get_time_constraint(account_id, constraint_id)
