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
    OverrideLayer,
    OverrideReason,
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


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _override(row) -> OccurrenceOverride:
    return OccurrenceOverride(
        id=row["id"], account_id=row["account_id"], template_id=row["template_id"],
        original_recurrence_id=row["original_recurrence_id"], action=OccurrenceOverrideAction(row["action"]),
        replacement_start_local=_local_dt(row["replacement_start_local"]),
        replacement_duration_minutes=(None if row["replacement_duration_minutes"] is None else int(row["replacement_duration_minutes"])),
        version=int(row["version"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        layer=OverrideLayer(row["layer"]), replacement_title=row["replacement_title"], location_text=row["location_text"],
        teacher=row["teacher"], note=row["note"], reason=None if row["reason"] is None else OverrideReason(row["reason"]),
    )


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


def resolve_local(
    local_value: datetime,
    timezone_name: str,
    policy: LocalTimeResolutionPolicy = LocalTimeResolutionPolicy.EARLIER_FOLD_SHIFT_FORWARD,
) -> datetime:
    """Public deterministic local-civil-time resolution shared by recurrence owners."""
    return _resolve_local(local_value, timezone_name, policy)


class _Effective:
    """One occurrence as the layers leave it: SOURCE applies first, USER on top."""

    def __init__(self, *, start_local: datetime, duration: int, title: str, location_text: str | None, teacher: str | None) -> None:
        self.start_local = start_local
        self.duration = duration
        self.title = title
        self.location_text = location_text
        self.teacher = teacher
        self.note: str | None = None
        self.cancelled_by: OverrideLayer | None = None
        self.cancel_reason: OverrideReason | None = None

    def apply(self, item: OccurrenceOverride) -> None:
        if item.action is OccurrenceOverrideAction.CANCEL:
            self.cancelled_by = item.layer
            self.cancel_reason = item.reason
            if item.note is not None:
                self.note = item.note
            return
        # A USER change on top of a SOURCE cancel does not bring the class back: the
        # timetable says it is not happening. Details still apply for display.
        if item.replacement_start_local is not None:
            self.start_local = item.replacement_start_local
        if item.replacement_duration_minutes is not None:
            self.duration = item.replacement_duration_minutes
        if item.replacement_title is not None:
            self.title = item.replacement_title
        if item.location_text is not None:
            self.location_text = item.location_text
        if item.teacher is not None:
            self.teacher = item.teacher
        if item.note is not None:
            self.note = item.note


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
        location_text: str | None = None,
        teacher: str | None = None,
        source_system_id: str | None = None,
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
            location_text=_text(location_text),
            teacher=_text(teacher),
            source_system_id=source_system_id,
        )
        # Validate timezone/policy immediately.
        _resolve_local(template.dtstart_local, template.timezone_name, template.resolution_policy)
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO recurring_templates("
                "id,account_id,title,description,category,importance,dtstart_local,duration_minutes,recurrence_rule,timezone_name,"
                "attendance_policy,location_effect_kind,origin_place_id,destination_place_id,arrival_requirement_minutes,"
                "resolution_policy,series_end_before_local,version,created_at,updated_at,location_text,teacher,source_system_id"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    template.id, account_id, title, description, category.value, importance.value,
                    _local_iso(dtstart_local), duration_minutes, rule.canonical(), timezone_name,
                    attendance_policy.value, resolved_location.kind.value, resolved_location.origin_place_id,
                    resolved_location.destination_place_id, arrival_requirement_minutes, resolution_policy.value,
                    None, 1, _iso(now), _iso(now), template.location_text, template.teacher, source_system_id,
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
            location_text=row["location_text"], teacher=row["teacher"], source_system_id=row["source_system_id"],
        )

    def list_templates(self, account_id: str) -> list[RecurringTemplate]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT id FROM recurring_templates WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [self.get_template(account_id, row["id"]) for row in rows]

    def get_override(
        self, account_id: str, template_id: str, original_recurrence_id: str,
        layer: OverrideLayer = OverrideLayer.USER,
    ) -> OccurrenceOverride | None:
        row = self.connection.execute(
            "SELECT * FROM occurrence_overrides WHERE account_id=? AND template_id=? AND original_recurrence_id=? AND layer=?",
            (account_id, template_id, original_recurrence_id, layer.value),
        ).fetchone()
        return None if row is None else _override(row)

    def list_overrides(self, account_id: str, template_id: str) -> list[OccurrenceOverride]:
        rows = self.connection.execute(
            "SELECT * FROM occurrence_overrides WHERE account_id=? AND template_id=? ORDER BY original_recurrence_id, layer",
            (account_id, template_id),
        ).fetchall()
        return [_override(row) for row in rows]

    def remove_override(
        self, *, account_id: str, template_id: str, original_recurrence_id: str, actor: ActorCategory,
        layer: OverrideLayer = OverrideLayer.USER,
    ) -> bool:
        """Drop one layer's change of an occurrence ("restore"); the other layer stays."""
        existing = self.get_override(account_id, template_id, original_recurrence_id, layer)
        if existing is None:
            return False
        with self.canonical._tx() as conn:
            conn.execute("DELETE FROM occurrence_overrides WHERE id=?", (existing.id,))
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="OCCURRENCE_OVERRIDE", entity_id=existing.id,
                action="REMOVE_OCCURRENCE_OVERRIDE", actor=actor,
                payload={"template_id": template_id, "original_recurrence_id": original_recurrence_id, "layer": layer.value},
            )
        return True

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
        layer: OverrideLayer = OverrideLayer.USER,
        replacement_title: str | None = None,
        location_text: str | None = None,
        teacher: str | None = None,
        note: str | None = None,
        reason: OverrideReason | None = None,
    ) -> OccurrenceOverride:
        template = self.get_template(account_id, template_id)
        try:
            original_local = datetime.fromisoformat(original_recurrence_id)
        except ValueError as exc:
            raise ValidationError("original_recurrence_id must be a local ISO date-time") from exc
        # Prove this identity belongs to the series before accepting an override.
        if not self._series_contains_original(template, original_local):
            raise ValidationError("occurrence identity is not part of the recurrence series")
        existing = self.get_override(account_id, template_id, original_recurrence_id, layer)
        now = self.clock.now()
        if reason is None:
            reason = OverrideReason.SOURCE if layer is OverrideLayer.SOURCE else OverrideReason.USER
        candidate = OccurrenceOverride(
            id=(existing.id if existing else (override_id or str(uuid4()))), account_id=account_id,
            template_id=template_id, original_recurrence_id=original_recurrence_id, action=action,
            replacement_start_local=replacement_start_local, replacement_duration_minutes=replacement_duration_minutes,
            version=(1 if existing is None else existing.version + 1),
            created_at=(now if existing is None else existing.created_at), updated_at=now,
            layer=layer, replacement_title=_text(replacement_title), location_text=_text(location_text),
            teacher=_text(teacher), note=_text(note), reason=reason,
        )
        if replacement_start_local is not None:
            _resolve_local(replacement_start_local, template.timezone_name, template.resolution_policy)
        with self.canonical._tx() as conn:
            if existing is None:
                if expected_version not in (None, 0):
                    raise VersionConflict("occurrence override does not exist")
                conn.execute(
                    "INSERT INTO occurrence_overrides(id,account_id,template_id,original_recurrence_id,layer,action,replacement_start_local,"
                    "replacement_duration_minutes,replacement_title,location_text,teacher,note,reason,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate.id, account_id, template_id, original_recurrence_id, layer.value, action.value,
                     _local_iso(replacement_start_local), replacement_duration_minutes, candidate.replacement_title,
                     candidate.location_text, candidate.teacher, candidate.note, reason.value, 1, _iso(now), _iso(now)),
                )
            else:
                if expected_version is not None and expected_version != existing.version:
                    raise VersionConflict("occurrence override version changed")
                cur = conn.execute(
                    "UPDATE occurrence_overrides SET action=?,replacement_start_local=?,replacement_duration_minutes=?,replacement_title=?,"
                    "location_text=?,teacher=?,note=?,reason=?,version=version+1,updated_at=? "
                    "WHERE account_id=? AND template_id=? AND original_recurrence_id=? AND layer=? AND version=?",
                    (action.value, _local_iso(replacement_start_local), replacement_duration_minutes, candidate.replacement_title,
                     candidate.location_text, candidate.teacher, candidate.note, reason.value, _iso(now),
                     account_id, template_id, original_recurrence_id, layer.value, existing.version),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("occurrence override version changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="OCCURRENCE_OVERRIDE",
                entity_id=candidate.id, action="SET_OCCURRENCE_OVERRIDE", actor=actor,
                payload={"template_id": template_id, "original_recurrence_id": original_recurrence_id,
                         "action": action.value, "layer": layer.value, "reason": reason.value},
            )
        return self.get_override(account_id, template_id, original_recurrence_id, layer)  # type: ignore[return-value]

    _UPDATABLE = {"title", "description", "category", "importance", "dtstart_local", "duration_minutes",
                  "recurrence_rule", "attendance_policy", "location_text", "teacher"}

    def update_template(
        self, *, account_id: str, template_id: str, fields: dict, actor: ActorCategory,
        expected_version: int | None = None,
    ) -> RecurringTemplate:
        """Change series-wide fields. Occurrence identities stay anchored to their original
        local start, so a USER override of a class keeps pointing at the same class."""
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise ValidationError("series fields cannot be changed: " + ", ".join(sorted(unknown)))
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict("recurring template version changed")
        before = {
            "title": current.title, "description": current.description, "category": current.category.value,
            "importance": current.importance.value, "dtstart_local": _local_iso(current.dtstart_local),
            "duration_minutes": current.duration_minutes, "recurrence_rule": current.recurrence_rule.canonical(),
            "attendance_policy": current.attendance_policy.value, "location_text": current.location_text,
            "teacher": current.teacher,
        }
        values = dict(before)
        for key, value in fields.items():
            if key == "dtstart_local":
                value = _local_iso(value)
            elif key == "recurrence_rule":
                value = (value if isinstance(value, RecurrenceRule) else RecurrenceRule.parse(str(value))).canonical()
            elif key in {"category", "importance", "attendance_policy"} and hasattr(value, "value"):
                value = value.value
            elif key in {"location_text", "teacher", "description"}:
                value = _text(value)
            values[key] = value
        if not str(values["title"] or "").strip():
            raise ValidationError("series title is required")
        if int(values["duration_minutes"]) <= 0:
            raise ValidationError("recurring duration must be positive")
        ObligationCategory(values["category"]); Importance(values["importance"]); AttendancePolicy(values["attendance_policy"])
        _resolve_local(datetime.fromisoformat(values["dtstart_local"]), current.timezone_name, current.resolution_policy)
        changed = {key for key in fields if values[key] != before[key]}
        if not changed:
            return current
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE recurring_templates SET title=?,description=?,category=?,importance=?,dtstart_local=?,duration_minutes=?,"
                "recurrence_rule=?,attendance_policy=?,location_text=?,teacher=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (values["title"], values["description"], values["category"], values["importance"], values["dtstart_local"],
                 int(values["duration_minutes"]), values["recurrence_rule"], values["attendance_policy"], values["location_text"],
                 values["teacher"], _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("recurring template version changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="RECURRING_TEMPLATE", entity_id=template_id,
                action="UPDATE_RECURRING_TEMPLATE", actor=actor, payload={"fields": sorted(changed)},
            )
        return self.get_template(account_id, template_id)

    def end_series(self, *, account_id: str, template_id: str, before_local: datetime, actor: ActorCategory) -> RecurringTemplate:
        """No occurrences from `before_local` on; earlier ones (history) stay."""
        current = self.get_template(account_id, template_id)
        if current.series_end_before_local is not None and current.series_end_before_local <= before_local:
            return current
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE recurring_templates SET series_end_before_local=?,version=version+1,updated_at=? WHERE account_id=? AND id=?",
                (_local_iso(before_local), _iso(now), account_id, template_id),
            )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="RECURRING_TEMPLATE", entity_id=template_id,
                action="END_RECURRING_TEMPLATE", actor=actor, payload={"before_local": _local_iso(before_local)},
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
        layers: dict[str, dict[OverrideLayer, OccurrenceOverride]] = {}
        for item in self.list_overrides(account_id, template_id):
            layers.setdefault(item.original_recurrence_id, {})[item.layer] = item
        # A moved occurrence can land inside the horizon from an original position after it,
        # so an open-ended series is scanned at least up to the last such original.
        relevant_moved_originals = []
        for rid, by_layer in layers.items():
            for item in by_layer.values():
                if item.replacement_start_local is None:
                    continue
                replacement_start = _resolve_local(item.replacement_start_local, template.timezone_name, template.resolution_policy)
                if replacement_start.astimezone(timezone.utc) < horizon_end.astimezone(timezone.utc):
                    relevant_moved_originals.append(datetime.fromisoformat(rid))
        last_moved_original = max(relevant_moved_originals, default=None)
        result: list[RecurringOccurrence] = []
        for original_local in self._iter_original_locals(template):
            rid = recurrence_id(original_local)
            effective = _Effective(start_local=original_local, duration=template.duration_minutes, title=template.title,
                                   location_text=template.location_text, teacher=template.teacher)
            by_layer = layers.get(rid, {})
            for layer in (OverrideLayer.SOURCE, OverrideLayer.USER):
                item = by_layer.get(layer)
                if item is not None:
                    effective.apply(item)
            starts = _resolve_local(effective.start_local, template.timezone_name, template.resolution_policy)
            ends = starts + timedelta(minutes=effective.duration)
            if ends.astimezone(timezone.utc) <= horizon_start.astimezone(timezone.utc):
                continue
            if starts.astimezone(timezone.utc) >= horizon_end.astimezone(timezone.utc):
                if (
                    template.recurrence_rule.count is None
                    and template.recurrence_rule.until_local is None
                    and (last_moved_original is None or original_local >= last_moved_original)
                ):
                    break
                continue
            user = by_layer.get(OverrideLayer.USER)
            source = by_layer.get(OverrideLayer.SOURCE)
            result.append(RecurringOccurrence(
                template_id=template.id, original_recurrence_id=rid,
                starts_at=starts, ends_at=ends, cancelled=effective.cancelled_by is not None,
                override_id=(user or source).id if (user or source) else None,
                title=effective.title, location_text=effective.location_text, teacher=effective.teacher,
                note=effective.note, cancelled_by=effective.cancelled_by, cancel_reason=effective.cancel_reason,
                changed_by=tuple(layer for layer in (OverrideLayer.SOURCE, OverrideLayer.USER) if layer in by_layer),
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
                    category=template.category, title=occurrence.title or template.title, description=template.description,
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
