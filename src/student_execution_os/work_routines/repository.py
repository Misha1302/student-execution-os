from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    LifecycleStatus,
    ObligationCategory,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.recurrence import RecurrenceFrequency, RecurrenceRule, recurrence_id, resolve_local

from .model import WorkRoutineOccurrence, WorkRoutineTemplate


def _local_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _local_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        raise ValidationError("local routine datetime must be naive")
    return value.isoformat()


def _step(rule: RecurrenceRule) -> timedelta:
    days = rule.interval if rule.frequency is RecurrenceFrequency.DAILY else 7 * rule.interval
    return timedelta(days=days)


def _task_id(template_id: str, original_recurrence_id: str) -> str:
    digest = hashlib.sha256(f"{template_id}|{original_recurrence_id}".encode("utf-8")).hexdigest()[:32]
    return f"routine-{digest}"


class SQLiteWorkRoutineRepository:
    """Recurring work materialized as canonical Task obligations.

    Template/original recurrence identity owns recurrence. The generated Task owns
    schedulable work, progress and execution, so no parallel planner entity exists.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def create_template(
        self,
        *,
        account_id: str,
        template_id: str,
        title: str,
        dtstart_local: datetime,
        effort_minutes: int,
        recurrence_rule: str,
        timezone_name: str,
        actor: ActorCategory,
        description: str | None = None,
        category: ObligationCategory = ObligationCategory.GENERAL,
        importance: Importance = Importance.NORMAL,
        splittable: bool = True,
        min_chunk_minutes: int | None = 15,
        max_chunk_minutes: int | None = 90,
    ) -> WorkRoutineTemplate:
        self.canonical._require_account(account_id)
        if dtstart_local.tzinfo is not None:
            raise ValidationError("dtstart_local must be local civil time without offset")
        if effort_minutes <= 0:
            raise ValidationError("effort_minutes must be positive")
        rule = RecurrenceRule.parse(recurrence_rule)
        resolve_local(dtstart_local, timezone_name)
        if not splittable:
            min_chunk_minutes = None
            max_chunk_minutes = None
        elif min_chunk_minutes is not None and max_chunk_minutes is not None and min_chunk_minutes > max_chunk_minutes:
            raise ValidationError("min chunk cannot exceed max chunk")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO work_routine_templates("
                "id,account_id,title,description,category,importance,dtstart_local,effort_minutes,"
                "recurrence_rule,timezone_name,splittable,min_chunk_minutes,max_chunk_minutes,status,"
                "series_end_before_local,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',NULL,1,?,?)",
                (
                    template_id, account_id, title, description, category.value, importance.value,
                    _local_iso(dtstart_local), effort_minutes, rule.canonical(), timezone_name,
                    int(splittable), min_chunk_minutes, max_chunk_minutes, _iso(now), _iso(now),
                ),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_TEMPLATE",
                entity_id=template_id, action="CREATE_WORK_ROUTINE", actor=actor,
            )
        return self.get_template(account_id, template_id)

    def get_template(self, account_id: str, template_id: str) -> WorkRoutineTemplate:
        row = self.connection.execute(
            "SELECT * FROM work_routine_templates WHERE account_id=? AND id=?", (account_id, template_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("work routine not found")
        return WorkRoutineTemplate(
            id=row["id"], account_id=row["account_id"], title=row["title"], description=row["description"],
            category=ObligationCategory(row["category"]), importance=Importance(row["importance"]),
            dtstart_local=_local_dt(row["dtstart_local"]), effort_minutes=int(row["effort_minutes"]),
            recurrence_rule=RecurrenceRule.parse(row["recurrence_rule"]), timezone_name=row["timezone_name"],
            splittable=bool(row["splittable"]), min_chunk_minutes=row["min_chunk_minutes"],
            max_chunk_minutes=row["max_chunk_minutes"], status=row["status"],
            series_end_before_local=_local_dt(row["series_end_before_local"]), version=int(row["version"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def list_templates(self, account_id: str) -> list[WorkRoutineTemplate]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT id FROM work_routine_templates WHERE account_id=? ORDER BY status,id", (account_id,)
        ).fetchall()
        return [self.get_template(account_id, row["id"]) for row in rows]

    def cancel_template(self, account_id: str, template_id: str, expected_version: int, actor: ActorCategory) -> WorkRoutineTemplate:
        current = self.get_template(account_id, template_id)
        if current.version != expected_version:
            raise VersionConflict(f"expected routine version {expected_version}, current {current.version}")
        if current.status == "CANCELLED":
            return current
        future_rows = self.connection.execute(
            "SELECT original_recurrence_id,task_id FROM work_routine_occurrences "
            "WHERE account_id=? AND template_id=? AND original_recurrence_id>=? ORDER BY original_recurrence_id",
            (account_id, template_id, original_recurrence_id),
        ).fetchall()
        for row in future_rows:
            task = self.canonical.get_task(account_id, row["task_id"])
            linked = self.connection.execute(
                "SELECT 1 FROM project_members WHERE account_id=? AND obligation_id=? LIMIT 1",
                (account_id, row["task_id"]),
            ).fetchone()
            attachment = self.connection.execute(
                "SELECT 1 FROM attachment_links WHERE account_id=? AND owner_kind='OBLIGATION' AND owner_id=? LIMIT 1",
                (account_id, row["task_id"]),
            ).fetchone()
            if (
                task.obligation.lifecycle_status not in {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}
                or task.obligation.version != 1
                or task.started_at is not None
                or task.last_progress_at is not None
                or linked is not None
                or attachment is not None
            ):
                raise VersionConflict("future routine occurrence has user history; split before an untouched occurrence")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            for row in future_rows:
                task = self.canonical.get_task(account_id, row["task_id"])
                self.canonical.delete_obligation(
                    account_id=account_id, obligation_id=row["task_id"],
                    expected_version=task.obligation.version, actor=actor,
                )
            cur = conn.execute(
                "UPDATE work_routine_templates SET status='CANCELLED',version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_iso(now), account_id, template_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("work routine version changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_TEMPLATE",
                entity_id=template_id, action="CANCEL_WORK_ROUTINE", actor=actor,
            )
        return self.get_template(account_id, template_id)

    def split_this_and_future(
        self,
        *,
        account_id: str,
        template_id: str,
        original_recurrence_id: str,
        successor_id: str,
        actor: ActorCategory,
        title: str | None = None,
        effort_minutes: int | None = None,
        replacement_start_local: datetime | None = None,
        recurrence_rule: str | RecurrenceRule | None = None,
        timezone_name: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[WorkRoutineTemplate, WorkRoutineTemplate]:
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict("work routine version changed")
        boundary = datetime.fromisoformat(original_recurrence_id)
        if not self._contains_original(current, boundary):
            raise ValidationError("split boundary is not an occurrence of this routine")
        next_rule = current.recurrence_rule if recurrence_rule is None else (
            recurrence_rule if isinstance(recurrence_rule, RecurrenceRule) else RecurrenceRule.parse(recurrence_rule)
        )
        if current.recurrence_rule.count is not None and recurrence_rule is None:
            raise ValidationError("splitting a COUNT-limited routine requires an explicit successor RRULE")
        next_timezone = timezone_name or current.timezone_name
        successor_start = replacement_start_local or boundary
        if successor_start.tzinfo is not None:
            raise ValidationError("replacement_start_local must be local civil time")
        resolve_local(successor_start, next_timezone)
        next_effort = current.effort_minutes if effort_minutes is None else int(effort_minutes)
        if next_effort <= 0:
            raise ValidationError("effort_minutes must be positive")
        next_title = current.title if title is None else title.strip()
        if not next_title:
            raise ValidationError("title is required")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE work_routine_templates SET series_end_before_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_local_iso(boundary), _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("work routine version changed before split")
            conn.execute(
                "INSERT INTO work_routine_templates("
                "id,account_id,title,description,category,importance,dtstart_local,effort_minutes,"
                "recurrence_rule,timezone_name,splittable,min_chunk_minutes,max_chunk_minutes,status,"
                "series_end_before_local,version,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',NULL,1,?,?)",
                (
                    successor_id, account_id, next_title, current.description,
                    current.category.value, current.importance.value, _local_iso(successor_start),
                    next_effort, next_rule.canonical(), next_timezone, int(current.splittable),
                    current.min_chunk_minutes, current.max_chunk_minutes, _iso(now), _iso(now),
                ),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_TEMPLATE",
                entity_id=template_id, action="SPLIT_WORK_ROUTINE", actor=actor,
                payload={"boundary_original_recurrence_id": original_recurrence_id, "successor_id": successor_id},
            )
        return self.get_template(account_id, template_id), self.get_template(account_id, successor_id)

    def _iter_originals(self, template: WorkRoutineTemplate, start_local: datetime | None = None):
        step = _step(template.recurrence_rule)
        index = 0
        if start_local is not None and start_local > template.dtstart_local:
            delta = start_local - template.dtstart_local
            index = max(0, delta.days // step.days - 1)
        current = template.dtstart_local + index * step
        count = index
        while True:
            rule = template.recurrence_rule
            if template.series_end_before_local is not None and current >= template.series_end_before_local:
                return
            if rule.until_local is not None and current > rule.until_local:
                return
            count += 1
            if rule.count is not None and count > rule.count:
                return
            yield current
            current += step

    def _contains_original(self, template: WorkRoutineTemplate, original_local: datetime) -> bool:
        if original_local.tzinfo is not None or original_local < template.dtstart_local:
            return False
        rule = template.recurrence_rule
        delta = original_local - template.dtstart_local
        step = _step(rule)
        if delta.seconds != 0 or delta.microseconds != 0 or delta.days % step.days != 0:
            return False
        index = delta.days // step.days
        if template.series_end_before_local is not None and original_local >= template.series_end_before_local:
            return False
        if rule.count is not None and index >= rule.count:
            return False
        if rule.until_local is not None and original_local > rule.until_local:
            return False
        return True

    def get_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str) -> WorkRoutineOccurrence | None:
        row = self.connection.execute(
            "SELECT * FROM work_routine_occurrences WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
            (account_id, template_id, original_recurrence_id),
        ).fetchone()
        if row is None:
            return None
        return WorkRoutineOccurrence(
            account_id=row["account_id"], template_id=row["template_id"],
            original_recurrence_id=row["original_recurrence_id"], task_id=row["task_id"],
            state=row["state"], override_title=row["override_title"],
            override_effort_minutes=row["override_effort_minutes"],
            override_target_local=_local_dt(row["override_target_local"]),
            version=int(row["version"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def _materialize(self, template: WorkRoutineTemplate, original_local: datetime) -> WorkRoutineOccurrence:
        rid = recurrence_id(original_local)
        existing = self.get_occurrence(template.account_id, template.id, rid)
        if existing is not None:
            return existing
        target = resolve_local(original_local, template.timezone_name)
        actionable = resolve_local(original_local - _step(template.recurrence_rule), template.timezone_name)
        if actionable < template.created_at:
            actionable = template.created_at
        task_id = _task_id(template.id, rid)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            task = self.canonical.create_task(
                account_id=template.account_id, obligation_id=task_id,
                title=template.title, description=template.description,
                category=template.category, importance=template.importance,
                estimated_total_effort_minutes=template.effort_minutes,
                remaining_effort_minutes=template.effort_minutes,
                splittable=template.splittable,
                min_chunk_minutes=template.min_chunk_minutes,
                max_chunk_minutes=template.max_chunk_minutes,
                actionable_from=actionable,
                target_at=target,
                actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.SYSTEM,
            )
            conn.execute(
                "INSERT INTO work_routine_occurrences("
                "account_id,template_id,original_recurrence_id,task_id,state,version,created_at,updated_at"
                ") VALUES (?,?,?,?,'ACTIVE',1,?,?)",
                (template.account_id, template.id, rid, task.obligation.id, _iso(now), _iso(now)),
            )
            self.canonical._record_change(
                conn, account_id=template.account_id, entity_type="WORK_ROUTINE_OCCURRENCE",
                entity_id=f"{template.id}:{rid}", action="MATERIALIZE_WORK_ROUTINE_OCCURRENCE",
                actor=ActorCategory.SYSTEM, payload={"task_id": task.obligation.id},
            )
        return self.get_occurrence(template.account_id, template.id, rid)  # type: ignore[return-value]

    def ensure_horizon(self, account_id: str, horizon_start: datetime, horizon_end: datetime) -> list[WorkRoutineOccurrence]:
        if horizon_start.tzinfo is None or horizon_end.tzinfo is None or horizon_start >= horizon_end:
            raise ValidationError("routine horizon must be a non-empty aware interval")
        out: list[WorkRoutineOccurrence] = []
        for template in self.list_templates(account_id):
            if template.status != "ACTIVE":
                continue
            local_start = horizon_start.astimezone(ZoneInfo(template.timezone_name)).replace(tzinfo=None)
            for original in self._iter_originals(template, local_start):
                target = resolve_local(original, template.timezone_name)
                if target.astimezone(timezone.utc) < horizon_start.astimezone(timezone.utc):
                    continue
                if target.astimezone(timezone.utc) >= horizon_end.astimezone(timezone.utc):
                    break
                out.append(self._materialize(template, original))
        return out

    def _require_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str) -> WorkRoutineOccurrence:
        template = self.get_template(account_id, template_id)
        original = datetime.fromisoformat(original_recurrence_id)
        if not self._contains_original(template, original):
            raise ValidationError("occurrence identity is not part of the work routine")
        return self.get_occurrence(account_id, template_id, original_recurrence_id) or self._materialize(template, original)

    def skip_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str, actor: ActorCategory) -> WorkRoutineOccurrence:
        occurrence = self._require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.state == "SKIPPED":
            return occurrence
        task = self.canonical.get_task(account_id, occurrence.task_id)
        if task.obligation.lifecycle_status is LifecycleStatus.COMPLETED:
            raise VersionConflict("completed routine occurrence cannot be skipped")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            if task.obligation.lifecycle_status in {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}:
                self.canonical.cancel_obligation(
                    account_id=account_id, obligation_id=occurrence.task_id,
                    expected_version=task.obligation.version, actor=actor,
                )
            conn.execute(
                "UPDATE work_routine_occurrences SET state='SKIPPED',version=version+1,updated_at=? "
                "WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (_iso(now), account_id, template_id, original_recurrence_id),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_OCCURRENCE",
                entity_id=f"{template_id}:{original_recurrence_id}", action="SKIP_WORK_ROUTINE_OCCURRENCE", actor=actor,
            )
        return self.get_occurrence(account_id, template_id, original_recurrence_id)  # type: ignore[return-value]

    def reopen_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str, actor: ActorCategory) -> WorkRoutineOccurrence:
        occurrence = self._require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.state == "ACTIVE":
            return occurrence
        task = self.canonical.get_task(account_id, occurrence.task_id)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            if task.obligation.lifecycle_status is LifecycleStatus.CANCELLED:
                self.canonical.reopen_obligation(
                    account_id=account_id, obligation_id=occurrence.task_id,
                    expected_version=task.obligation.version, actor=actor,
                )
            conn.execute(
                "UPDATE work_routine_occurrences SET state='ACTIVE',version=version+1,updated_at=? "
                "WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (_iso(now), account_id, template_id, original_recurrence_id),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_OCCURRENCE",
                entity_id=f"{template_id}:{original_recurrence_id}", action="REOPEN_WORK_ROUTINE_OCCURRENCE", actor=actor,
            )
        return self.get_occurrence(account_id, template_id, original_recurrence_id)  # type: ignore[return-value]

    def edit_occurrence(
        self,
        account_id: str,
        template_id: str,
        original_recurrence_id: str,
        *,
        actor: ActorCategory,
        title: str | None = None,
        effort_minutes: int | None = None,
        target_local: datetime | None = None,
    ) -> WorkRoutineOccurrence:
        occurrence = self._require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.state != "ACTIVE":
            raise VersionConflict("reopen a skipped occurrence before editing it")
        template = self.get_template(account_id, template_id)
        task = self.canonical.get_task(account_id, occurrence.task_id)
        if task.obligation.lifecycle_status not in {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}:
            raise VersionConflict("closed routine occurrence cannot be edited")
        if target_local is not None and target_local.tzinfo is not None:
            raise ValidationError("target_local must be local civil time without offset")
        if effort_minutes is not None and effort_minutes <= 0:
            raise ValidationError("effort_minutes must be positive")
        if effort_minutes is not None and task.started_at is not None:
            raise VersionConflict("started routine work keeps its effort history; edit remaining effort instead")
        effective_target_local = target_local or occurrence.override_target_local or datetime.fromisoformat(original_recurrence_id)
        target = resolve_local(effective_target_local, template.timezone_name)
        fields = {"target_at": target}
        if title is not None:
            if not title.strip():
                raise ValidationError("title cannot be empty")
            fields["title"] = title.strip()
        if effort_minutes is not None:
            fields["estimated_total_effort_minutes"] = effort_minutes
            fields["remaining_effort_minutes"] = effort_minutes
        now = self.clock.now()
        with self.canonical._tx() as conn:
            self.canonical.update_task(
                account_id=account_id, obligation_id=occurrence.task_id,
                expected_version=task.obligation.version, actor=actor, **fields,
            )
            conn.execute(
                "UPDATE work_routine_occurrences SET override_title=?,override_effort_minutes=?,"
                "override_target_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (
                    title if title is not None else occurrence.override_title,
                    effort_minutes if effort_minutes is not None else occurrence.override_effort_minutes,
                    _local_iso(target_local) if target_local is not None else _local_iso(occurrence.override_target_local),
                    _iso(now), account_id, template_id, original_recurrence_id,
                ),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="WORK_ROUTINE_OCCURRENCE",
                entity_id=f"{template_id}:{original_recurrence_id}", action="EDIT_WORK_ROUTINE_OCCURRENCE", actor=actor,
            )
        return self.get_occurrence(account_id, template_id, original_recurrence_id)  # type: ignore[return-value]

    def list_occurrences(self, account_id: str, template_id: str) -> list[WorkRoutineOccurrence]:
        self.get_template(account_id, template_id)
        rows = self.connection.execute(
            "SELECT original_recurrence_id FROM work_routine_occurrences "
            "WHERE account_id=? AND template_id=? ORDER BY original_recurrence_id",
            (account_id, template_id),
        ).fetchall()
        return [
            self.get_occurrence(account_id, template_id, row["original_recurrence_id"])
            for row in rows
        ]  # type: ignore[list-item]
