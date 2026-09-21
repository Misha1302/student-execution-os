from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    Event,
    EventTimeSemantics,
    HalfOpenInterval,
    Importance,
    LifecycleStatus,
    LocationEffect,
    LocationEffectKind,
    Obligation,
    ObligationCategory,
    ObligationKind,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .model import (
    LocalTimeResolutionPolicy,
    OccurrenceOverride,
    OccurrenceOverrideAction,
    RecurrenceFrequency,
    RecurrenceRule,
    RecurringOccurrence,
    RecurringTemplate,
)


def _local_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        raise ValidationError("local civil datetime must be naive")
    return value.isoformat()


def _local_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def recurrence_id(local_start: datetime) -> str:
    if local_start.tzinfo is not None:
        raise ValidationError("recurrence id must be based on naive local civil time")
    return local_start.isoformat()


def _resolve_local(local_value: datetime, timezone_name: str, policy: LocalTimeResolutionPolicy) -> datetime:
    if local_value.tzinfo is not None:
        raise ValidationError("local recurrence value must be naive")
    if policy is not LocalTimeResolutionPolicy.EARLIER_FOLD_SHIFT_FORWARD:
        raise ValidationError("unsupported local-time resolution policy")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("unknown IANA timezone") from exc

    def valid(candidate_local: datetime, fold: int) -> datetime | None:
        aware = candidate_local.replace(tzinfo=zone, fold=fold)
        roundtrip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        return aware if roundtrip == candidate_local else None

    first = valid(local_value, 0)
    second = valid(local_value, 1)
    if first is not None:
        return first
    if second is not None:
        return second

    # Nonexistent local time (spring-forward gap): deterministic shift to first
    # valid local minute. Identity remains anchored to the original local time.
    probe = local_value
    for _ in range(180):
        probe += timedelta(minutes=1)
        found = valid(probe, 0) or valid(probe, 1)
        if found is not None:
            return found
    raise ValidationError("could not resolve local civil time within DST gap bound")


class SQLiteRecurrenceRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def create_template(
        self,
        *,
        account_id: str,
        title: str,
        dtstart_local: datetime,
        duration_minutes: int,
        recurrence_rule: str | RecurrenceRule,
        timezone_name: str,
        actor: ActorCategory,
        template_id: str | None = None,
        description: str | None = None,
        category: ObligationCategory = ObligationCategory.GENERAL,
        importance: Importance = Importance.NORMAL,
        attendance_policy: AttendancePolicy = AttendancePolicy.REQUIRED,
        location_effect: LocationEffect | None = None,
        arrival_requirement_minutes: int = 0,
        resolution_policy: LocalTimeResolutionPolicy = LocalTimeResolutionPolicy.EARLIER_FOLD_SHIFT_FORWARD,
    ) -> RecurringTemplate:
        self.canonical._require_account(account_id)
        rule = recurrence_rule if isinstance(recurrence_rule, RecurrenceRule) else RecurrenceRule.parse(recurrence_rule)
        resolved_location = location_effect or LocationEffect()
        self.canonical._validate_location_effect_places(account_id=account_id, location_effect=resolved_location)
        now = self.clock.now()
        template = RecurringTemplate(
            id=template_id or str(uuid4()),
            account_id=account_id,
            title=title,
            description=description,
            category=category,
            importance=importance,
            dtstart_local=dtstart_local,
            duration_minutes=duration_minutes,
            recurrence_rule=rule,
            timezone_name=timezone_name,
            attendance_policy=attendance_policy,
            location_effect=resolved_location,
            arrival_requirement_minutes=arrival_requirement_minutes,
            resolution_policy=resolution_policy,
            series_end_before_local=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        # Validate timezone/policy immediately.
        _resolve_local(template.dtstart_local, template.timezone_name, template.resolution_policy)
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO recurring_templates("
                "id,account_id,title,description,category,importance,dtstart_local,duration_minutes,recurrence_rule,timezone_name,"
                "attendance_policy,location_effect_kind,origin_place_id,destination_place_id,arrival_requirement_minutes,"
                "resolution_policy,series_end_before_local,version,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    template.id, account_id, title, description, category.value, importance.value,
                    _local_iso(dtstart_local), duration_minutes, rule.canonical(), timezone_name,
                    attendance_policy.value, resolved_location.kind.value, resolved_location.origin_place_id,
                    resolved_location.destination_place_id, arrival_requirement_minutes, resolution_policy.value,
                    None, 1, _iso(now), _iso(now),
                ),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="RECURRING_TEMPLATE",
                entity_id=template.id, action="CREATE_RECURRING_TEMPLATE", actor=actor,
            )
        return template

    def get_template(self, account_id: str, template_id: str) -> RecurringTemplate:
        row = self.connection.execute(
            "SELECT * FROM recurring_templates WHERE account_id=? AND id=?",
            (account_id, template_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("recurring template not found")
        return RecurringTemplate(
            id=row["id"], account_id=row["account_id"], title=row["title"], description=row["description"],
            category=ObligationCategory(row["category"]), importance=Importance(row["importance"]),
            dtstart_local=_local_dt(row["dtstart_local"]), duration_minutes=int(row["duration_minutes"]),
            recurrence_rule=RecurrenceRule.parse(row["recurrence_rule"]), timezone_name=row["timezone_name"],
            attendance_policy=AttendancePolicy(row["attendance_policy"]),
            location_effect=LocationEffect(LocationEffectKind(row["location_effect_kind"]), row["origin_place_id"], row["destination_place_id"]),
            arrival_requirement_minutes=int(row["arrival_requirement_minutes"]),
            resolution_policy=LocalTimeResolutionPolicy(row["resolution_policy"]),
            series_end_before_local=_local_dt(row["series_end_before_local"]), version=int(row["version"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def list_templates(self, account_id: str) -> list[RecurringTemplate]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT id FROM recurring_templates WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [self.get_template(account_id, row["id"]) for row in rows]

    def get_override(self, account_id: str, template_id: str, original_recurrence_id: str) -> OccurrenceOverride | None:
        row = self.connection.execute(
            "SELECT * FROM occurrence_overrides WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
            (account_id, template_id, original_recurrence_id),
        ).fetchone()
        if row is None:
            return None
        return OccurrenceOverride(
            id=row["id"], account_id=row["account_id"], template_id=row["template_id"],
            original_recurrence_id=row["original_recurrence_id"], action=OccurrenceOverrideAction(row["action"]),
            replacement_start_local=_local_dt(row["replacement_start_local"]),
            replacement_duration_minutes=(None if row["replacement_duration_minutes"] is None else int(row["replacement_duration_minutes"])),
            version=int(row["version"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def set_override(
        self,
        *,
        account_id: str,
        template_id: str,
        original_recurrence_id: str,
        action: OccurrenceOverrideAction,
        actor: ActorCategory,
        replacement_start_local: datetime | None = None,
        replacement_duration_minutes: int | None = None,
        expected_version: int | None = None,
        override_id: str | None = None,
    ) -> OccurrenceOverride:
        template = self.get_template(account_id, template_id)
        original_local = datetime.fromisoformat(original_recurrence_id)
        # Prove this identity belongs to the series before accepting an override.
        if not self._series_contains_original(template, original_local):
            raise ValidationError("occurrence identity is not part of the recurrence series")
        existing = self.get_override(account_id, template_id, original_recurrence_id)
        now = self.clock.now()
        candidate = OccurrenceOverride(
            id=(existing.id if existing else (override_id or str(uuid4()))), account_id=account_id,
            template_id=template_id, original_recurrence_id=original_recurrence_id, action=action,
            replacement_start_local=replacement_start_local, replacement_duration_minutes=replacement_duration_minutes,
            version=(1 if existing is None else existing.version + 1),
            created_at=(now if existing is None else existing.created_at), updated_at=now,
        )
        if replacement_start_local is not None:
            _resolve_local(replacement_start_local, template.timezone_name, template.resolution_policy)
        with self.canonical._tx() as conn:
            if existing is None:
                if expected_version not in (None, 0):
                    raise VersionConflict("occurrence override does not exist")
                conn.execute(
                    "INSERT INTO occurrence_overrides(id,account_id,template_id,original_recurrence_id,action,replacement_start_local,replacement_duration_minutes,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (candidate.id, account_id, template_id, original_recurrence_id, action.value,
                     _local_iso(replacement_start_local), replacement_duration_minutes, 1, _iso(now), _iso(now)),
                )
            else:
                if expected_version is not None and expected_version != existing.version:
                    raise VersionConflict("occurrence override version changed")
                cur = conn.execute(
                    "UPDATE occurrence_overrides SET action=?,replacement_start_local=?,replacement_duration_minutes=?,version=version+1,updated_at=? "
                    "WHERE account_id=? AND template_id=? AND original_recurrence_id=? AND version=?",
                    (action.value, _local_iso(replacement_start_local), replacement_duration_minutes, _iso(now),
                     account_id, template_id, original_recurrence_id, existing.version),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("occurrence override version changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="OCCURRENCE_OVERRIDE",
                entity_id=candidate.id, action="SET_OCCURRENCE_OVERRIDE", actor=actor,
                payload={"template_id": template_id, "original_recurrence_id": original_recurrence_id, "action": action.value},
            )
        return self.get_override(account_id, template_id, original_recurrence_id)  # type: ignore[return-value]

    def split_this_and_future(
        self,
        *,
        account_id: str,
        template_id: str,
        original_recurrence_id: str,
        successor_id: str,
        actor: ActorCategory,
        replacement_start_local: datetime | None = None,
        recurrence_rule: str | RecurrenceRule | None = None,
        expected_version: int | None = None,
    ) -> tuple[RecurringTemplate, RecurringTemplate]:
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict("recurring template version changed")
        boundary = datetime.fromisoformat(original_recurrence_id)
        if not self._series_contains_original(current, boundary):
            raise ValidationError("split boundary is not an occurrence of this series")
        if current.series_end_before_local is not None and boundary >= current.series_end_before_local:
            raise ValidationError("split boundary is outside active series")
        successor_start = replacement_start_local or boundary
        next_rule = current.recurrence_rule if recurrence_rule is None else (
            recurrence_rule if isinstance(recurrence_rule, RecurrenceRule) else RecurrenceRule.parse(recurrence_rule)
        )
        if current.recurrence_rule.count is not None and recurrence_rule is None:
            raise ValidationError("splitting a COUNT-limited series requires an explicit successor RRULE")
        _resolve_local(successor_start, current.timezone_name, current.resolution_policy)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE recurring_templates SET series_end_before_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_local_iso(boundary), _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("recurring template version changed before split")
            conn.execute(
                "INSERT INTO recurring_templates("
                "id,account_id,title,description,category,importance,dtstart_local,duration_minutes,recurrence_rule,timezone_name,attendance_policy,"
                "location_effect_kind,origin_place_id,destination_place_id,arrival_requirement_minutes,resolution_policy,series_end_before_local,version,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (successor_id, account_id, current.title, current.description, current.category.value, current.importance.value,
                 _local_iso(successor_start), current.duration_minutes, next_rule.canonical(), current.timezone_name,
                 current.attendance_policy.value, current.location_effect.kind.value, current.location_effect.origin_place_id,
                 current.location_effect.destination_place_id, current.arrival_requirement_minutes, current.resolution_policy.value,
                 None, 1, _iso(now), _iso(now)),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="RECURRING_TEMPLATE", entity_id=template_id,
                action="SPLIT_RECURRING_TEMPLATE", actor=actor,
                payload={"boundary_original_recurrence_id": original_recurrence_id, "successor_id": successor_id},
            )
        return self.get_template(account_id, template_id), self.get_template(account_id, successor_id)

    def expand(self, *, account_id: str, template_id: str, horizon_start: datetime, horizon_end: datetime) -> list[RecurringOccurrence]:
        if horizon_start.tzinfo is None or horizon_end.tzinfo is None:
            raise ValidationError("recurrence horizon must be offset-aware")
        if horizon_start >= horizon_end:
            raise ValidationError("recurrence horizon requires start < end")
        template = self.get_template(account_id, template_id)
        moved_override_rows = self.connection.execute(
            "SELECT original_recurrence_id,replacement_start_local FROM occurrence_overrides "
            "WHERE account_id=? AND template_id=? AND action='MODIFY'",
            (account_id, template_id),
        ).fetchall()
        relevant_moved_originals = []
        for row in moved_override_rows:
            replacement = _local_dt(row["replacement_start_local"])
            if replacement is None:
                continue
            replacement_start = _resolve_local(replacement, template.timezone_name, template.resolution_policy)
            if replacement_start.astimezone(timezone.utc) < horizon_end.astimezone(timezone.utc):
                relevant_moved_originals.append(datetime.fromisoformat(row["original_recurrence_id"]))
        last_moved_original = max(relevant_moved_originals, default=None)
        result: list[RecurringOccurrence] = []
        for original_local in self._iter_original_locals(template):
            rid = recurrence_id(original_local)
            override = self.get_override(account_id, template.id, rid)
            effective_local = original_local
            duration = template.duration_minutes
            cancelled = False
            override_id = None
            if override is not None:
                override_id = override.id
                if override.action is OccurrenceOverrideAction.CANCEL:
                    cancelled = True
                else:
                    assert override.replacement_start_local is not None
                    effective_local = override.replacement_start_local
                    duration = override.replacement_duration_minutes or duration
            starts = _resolve_local(effective_local, template.timezone_name, template.resolution_policy)
            ends = starts + timedelta(minutes=duration)
            if ends.astimezone(timezone.utc) <= horizon_start.astimezone(timezone.utc):
                continue
            if starts.astimezone(timezone.utc) >= horizon_end.astimezone(timezone.utc):
                # Cannot break for moved overrides, but finite horizon means later original positions
                # can still be relevant only if overrides move them backwards. Keep scanning within rule bounds.
                if (
                    template.recurrence_rule.count is None
                    and template.recurrence_rule.until_local is None
                    and (last_moved_original is None or original_local >= last_moved_original)
                ):
                    break
                continue
            result.append(RecurringOccurrence(
                template_id=template.id, original_recurrence_id=rid,
                starts_at=starts, ends_at=ends, cancelled=cancelled, override_id=override_id,
            ))
        return result

    def expand_as_events(self, *, account_id: str, horizon_start: datetime, horizon_end: datetime) -> list[Event]:
        events: list[Event] = []
        for template in self.list_templates(account_id):
            for occurrence in self.expand(
                account_id=account_id, template_id=template.id,
                horizon_start=horizon_start, horizon_end=horizon_end,
            ):
                if occurrence.cancelled:
                    continue
                occurrence_id = f"rec:{template.id}:{occurrence.original_recurrence_id}"
                obligation = Obligation(
                    id=occurrence_id, account_id=account_id, kind=ObligationKind.EVENT,
                    category=template.category, title=template.title, description=template.description,
                    lifecycle_status=LifecycleStatus.ACTIVE, importance=template.importance,
                    created_at=template.created_at, updated_at=template.updated_at,
                    completed_at=None, version=template.version,
                )
                events.append(Event(
                    obligation=obligation, time_semantics=EventTimeSemantics.FIXED_INTERVAL,
                    interval=HalfOpenInterval(occurrence.starts_at, occurrence.ends_at),
                    attendance_policy=template.attendance_policy, location_effect=template.location_effect,
                    arrival_requirement_minutes=template.arrival_requirement_minutes,
                ))
        return events

    def _iter_original_locals(self, template: RecurringTemplate):
        current = template.dtstart_local
        count = 0
        while True:
            if template.series_end_before_local is not None and current >= template.series_end_before_local:
                return
            if template.recurrence_rule.until_local is not None and current > template.recurrence_rule.until_local:
                return
            count += 1
            if template.recurrence_rule.count is not None and count > template.recurrence_rule.count:
                return
            yield current
            if template.recurrence_rule.frequency is RecurrenceFrequency.DAILY:
                current += timedelta(days=template.recurrence_rule.interval)
            else:
                current += timedelta(weeks=template.recurrence_rule.interval)

    def _series_contains_original(self, template: RecurringTemplate, original_local: datetime) -> bool:
        if original_local.tzinfo is not None or original_local < template.dtstart_local:
            return False
        if template.series_end_before_local is not None and original_local >= template.series_end_before_local:
            return False
        rule = template.recurrence_rule
        delta = original_local - template.dtstart_local
        unit_days = 1 if rule.frequency is RecurrenceFrequency.DAILY else 7
        step_days = unit_days * rule.interval
        if delta.seconds != 0 or delta.microseconds != 0 or delta.days % step_days != 0:
            return False
        index = delta.days // step_days
        if rule.count is not None and index >= rule.count:
            return False
        if rule.until_local is not None and original_local > rule.until_local:
            return False
        return True
