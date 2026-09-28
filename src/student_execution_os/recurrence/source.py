"""Apply an imported timetable to canonical state with stable external identity (ADR 0027).

A provider (iCalendar feed, a university timetable) turns its data into a `SourceSnapshot`:
series with their RECURRENCE-ID exceptions, and one-off events. This module is the only
writer of the SOURCE layer. It never touches what the user changed (the USER layer), never
deletes history, and never lets a late copy of an older update overwrite a newer one:

- identity: (source_system_id, UID, RECURRENCE-ID) -> local series / occurrence / event,
  in `external_identities`; local ids are derived from it, so re-imports are idempotent;
- ordering: (SEQUENCE, last-modified) per identity; an older update is counted as stale;
- removal: in a complete snapshot, a series missing from the source ends (past classes
  stay), a missing future one-off event is cancelled; it comes back if the source does.

Nothing here knows about any particular university: that is the provider's job.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    LifecycleStatus,
    LocationEffect,
    ObligationCategory,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .model import OccurrenceOverrideAction, OverrideLayer, OverrideReason, RecurrenceRule
from .repository import SQLiteRecurrenceRepository, recurrence_id


def set_event_details(repo: SQLiteCanonicalRepository, account_id: str, event_id: str, *, now: datetime,
                      location_text: str | None, teacher: str | None,
                      actor: ActorCategory = ActorCategory.USER_UI) -> bool:
    """Set one-off event details without producing churn on an identical refresh."""
    existing = repo.connection.execute(
        "SELECT location_text,teacher FROM event_details WHERE account_id=? AND event_id=?",
        (account_id, event_id),
    ).fetchone()
    before = None if existing is None else (existing["location_text"], existing["teacher"])
    after = None if location_text is None and teacher is None else (location_text, teacher)
    if before == after:
        return False
    if location_text is None and teacher is None:
        repo.connection.execute("DELETE FROM event_details WHERE account_id=? AND event_id=?", (account_id, event_id))
    else:
        repo.connection.execute(
            "INSERT INTO event_details(account_id,event_id,location_text,teacher,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id,event_id) DO UPDATE SET location_text=excluded.location_text,"
            "teacher=excluded.teacher,updated_at=excluded.updated_at",
            (account_id, event_id, location_text, teacher, _iso(now)),
        )
    repo._record_change(repo.connection, account_id=account_id, entity_type="OBLIGATION", entity_id=event_id,
                        action="UPDATE_EVENT_DETAILS", actor=actor)
    return True


@dataclass(frozen=True)
class SourceOccurrenceChange:
    """One RECURRENCE-ID instance: cancelled, or moved / changed details."""

    recurrence_local: datetime  # the original local start this instance replaces
    cancelled: bool = False
    starts_local: datetime | None = None
    duration_minutes: int | None = None
    title: str | None = None
    location_text: str | None = None
    teacher: str | None = None
    sequence: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SourceSeries:
    uid: str
    title: str
    dtstart_local: datetime
    duration_minutes: int
    recurrence_rule: str
    timezone_name: str
    category: ObligationCategory = ObligationCategory.LESSON
    description: str | None = None
    location_text: str | None = None
    teacher: str | None = None
    exdates_local: tuple[datetime, ...] = ()
    changes: tuple[SourceOccurrenceChange, ...] = ()
    sequence: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SourceEvent:
    """A one-off event. With `series_uid` it is an extra class of that series."""

    uid: str
    title: str
    starts_at: datetime
    ends_at: datetime
    category: ObligationCategory = ObligationCategory.LESSON
    description: str | None = None
    location_text: str | None = None
    teacher: str | None = None
    cancelled: bool = False
    series_uid: str | None = None
    sequence: int = 0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    source_system_id: str
    series: tuple[SourceSeries, ...] = ()
    events: tuple[SourceEvent, ...] = ()
    # A complete feed lists everything the source has: what is missing was removed. A
    # partial one (a single-day change feed) only adds and updates.
    complete: bool = True


@dataclass
class ApplyReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    occurrence_changes: int = 0
    ignored_changes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in (
            "created", "updated", "unchanged", "stale", "removed", "restored", "occurrence_changes", "ignored_changes")}


def local_id(prefix: str, source_system_id: str, uid: str, recurrence: str = "") -> str:
    digest = hashlib.sha256(f"{source_system_id}\x1f{uid}\x1f{recurrence}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class SourceApplier:
    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str,
                 actor: ActorCategory = ActorCategory.CONNECTOR_INGESTION) -> None:
        self.repo = repo
        self.account_id = account_id
        self.actor = actor
        self.recurrence = SQLiteRecurrenceRepository(repo)
        self.now = repo.clock.now()

    # ---- identity ---------------------------------------------------------------------

    def _identity(self, source: str, uid: str, recurrence: str = ""):
        return self.repo.connection.execute(
            "SELECT * FROM external_identities WHERE account_id=? AND source_system_id=? AND external_uid=? AND external_recurrence_id=?",
            (self.account_id, source, uid, recurrence),
        ).fetchone()

    @staticmethod
    def _is_stale(row, sequence: int, updated_at: datetime | None) -> bool:
        if row is None:
            return False
        if sequence != int(row["source_sequence"]):
            return sequence < int(row["source_sequence"])
        if updated_at is not None and row["source_updated_at"]:
            return updated_at < datetime.fromisoformat(row["source_updated_at"])
        return False

    def _remember(self, source: str, uid: str, recurrence: str, *, kind: str, local: str, sequence: int,
                  updated_at: datetime | None, template_id: str | None = None, original: str | None = None,
                  source_cancelled: bool = False) -> None:
        now = _iso(self.now)
        self.repo.connection.execute(
            "INSERT INTO external_identities(account_id,source_system_id,external_uid,external_recurrence_id,local_kind,local_id,"
            "template_id,original_recurrence_id,source_sequence,source_updated_at,state,source_cancelled,user_cancelled,"
            "first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,0,?,?) "
            "ON CONFLICT(account_id,source_system_id,external_uid,external_recurrence_id) DO UPDATE SET "
            "source_sequence=excluded.source_sequence,source_updated_at=excluded.source_updated_at,state='ACTIVE',"
            "source_cancelled=excluded.source_cancelled,last_seen_at=excluded.last_seen_at",
            (self.account_id, source, uid, recurrence, kind, local, template_id, original, sequence,
             None if updated_at is None else _iso(updated_at), int(source_cancelled), now, now),
        )

    def _touch(self, source: str, uid: str, recurrence: str = "") -> None:
        self.repo.connection.execute(
            "UPDATE external_identities SET last_seen_at=? WHERE account_id=? AND source_system_id=? AND external_uid=? "
            "AND external_recurrence_id=?", (_iso(self.now), self.account_id, source, uid, recurrence))

    # ---- apply ------------------------------------------------------------------------

    def apply(self, snapshot: SourceSnapshot) -> ApplyReport:
        if not snapshot.source_system_id:
            raise ValidationError("source_system_id is required")
        report = ApplyReport()
        seen: set[tuple[str, str]] = set()
        with self.repo._tx():
            for series in snapshot.series:
                self._apply_series(snapshot.source_system_id, series, report)
                seen.add((series.uid, ""))
            for event in snapshot.events:
                self._apply_event(snapshot.source_system_id, event, report)
                seen.add((event.uid, ""))
            if snapshot.complete:
                self._remove_missing(snapshot.source_system_id, seen, report)
        return report

    def _apply_series(self, source: str, item: SourceSeries, report: ApplyReport) -> None:
        RecurrenceRule.parse(item.recurrence_rule)
        row = self._identity(source, item.uid)
        if self._is_stale(row, item.sequence, item.updated_at):
            report.stale.append(item.uid)
            self._touch(source, item.uid)
            return
        fields = {
            "title": item.title.strip(), "description": item.description, "category": item.category,
            "dtstart_local": item.dtstart_local, "duration_minutes": item.duration_minutes,
            "recurrence_rule": item.recurrence_rule, "timezone_name": item.timezone_name,
            "location_text": item.location_text, "teacher": item.teacher,
        }
        if row is None:
            template_id = local_id("src-series", source, item.uid)
            template = self.recurrence.create_template(
                account_id=self.account_id, template_id=template_id, title=fields["title"],
                description=item.description, category=item.category, dtstart_local=item.dtstart_local,
                duration_minutes=item.duration_minutes, recurrence_rule=item.recurrence_rule,
                timezone_name=item.timezone_name, location_text=item.location_text, teacher=item.teacher,
                source_system_id=source, actor=self.actor,
            )
            report.created.append(item.uid)
        else:
            template_id = row["local_id"]
            before = self.recurrence.get_template(self.account_id, template_id)
            template = self.recurrence.update_template(account_id=self.account_id, template_id=template_id,
                                                       fields=fields, actor=self.actor)
            if template.dtstart_local != before.dtstart_local:
                self.recurrence.remap_user_overrides_for_source_shift(
                    account_id=self.account_id, template_id=template_id,
                    previous_template=before, actor=self.actor,
                )
            if row["state"] == "REMOVED":
                self._reopen_series(template_id)
                report.restored.append(item.uid)
            elif template.version != before.version:
                report.updated.append(item.uid)
            else:
                report.unchanged.append(item.uid)
        self._remember(source, item.uid, "", kind="SERIES", local=template_id, sequence=item.sequence,
                       updated_at=item.updated_at, template_id=template_id)
        report.occurrence_changes += self._apply_source_layer(source, template_id, item, report)

    def _apply_source_layer(self, source: str, template_id: str, item: SourceSeries, report: ApplyReport) -> int:
        """Make the SOURCE layer of this series equal to what the source says now."""
        template = self.recurrence.get_template(self.account_id, template_id)
        desired: dict[str, SourceOccurrenceChange] = {}
        for exdate in item.exdates_local:
            desired[recurrence_id(exdate)] = SourceOccurrenceChange(
                recurrence_local=exdate, cancelled=True, sequence=item.sequence, updated_at=item.updated_at)
        for change in item.changes:
            desired[recurrence_id(change.recurrence_local)] = change
        current = {o.original_recurrence_id: o for o in self.recurrence.list_overrides(self.account_id, template_id)
                   if o.layer is OverrideLayer.SOURCE}
        changed = 0
        for rid, override in current.items():
            if rid not in desired:
                self.recurrence.remove_override(account_id=self.account_id, template_id=template_id,
                                                original_recurrence_id=rid, actor=self.actor, layer=OverrideLayer.SOURCE)
                self._remember(source, item.uid, rid, kind="OCCURRENCE", local=f"rec:{template_id}:{rid}",
                               sequence=item.sequence, updated_at=item.updated_at,
                               template_id=template_id, original=rid)
                changed += 1
        for rid, change in desired.items():
            if not self.recurrence._series_contains_original(template, change.recurrence_local):
                report.ignored_changes.append(f"{item.uid}#{rid}")
                continue
            instance = self._identity(source, item.uid, rid)
            if self._is_stale(instance, change.sequence, change.updated_at):
                report.stale.append(f"{item.uid}#{rid}")
                continue
            wanted = self._override_fields(template, change)
            existing = current.get(rid)
            if not wanted:  # the instance says what the series says: no SOURCE change
                if existing is not None:
                    self.recurrence.remove_override(account_id=self.account_id, template_id=template_id,
                                                    original_recurrence_id=rid, actor=self.actor, layer=OverrideLayer.SOURCE)
                    changed += 1
                self._remember(source, item.uid, rid, kind="OCCURRENCE", local=f"rec:{template_id}:{rid}",
                               sequence=change.sequence, updated_at=change.updated_at,
                               template_id=template_id, original=rid)
                continue
            if existing is not None and self._same(existing, wanted):
                self._remember(source, item.uid, rid, kind="OCCURRENCE", local=f"rec:{template_id}:{rid}",
                               sequence=change.sequence, updated_at=change.updated_at,
                               template_id=template_id, original=rid)
                continue
            self.recurrence.set_override(
                account_id=self.account_id, template_id=template_id, original_recurrence_id=rid,
                layer=OverrideLayer.SOURCE, reason=OverrideReason.SOURCE, actor=self.actor, **wanted)
            self._remember(source, item.uid, rid, kind="OCCURRENCE", local=f"rec:{template_id}:{rid}",
                           sequence=change.sequence, updated_at=change.updated_at,
                           template_id=template_id, original=rid)
            changed += 1
        return changed

    @staticmethod
    def _override_fields(template, change: SourceOccurrenceChange) -> dict[str, object]:
        if change.cancelled:
            return {"action": OccurrenceOverrideAction.CANCEL}
        fields: dict[str, object] = {"action": OccurrenceOverrideAction.MODIFY}
        # Keep only what differs from the series, so a later series-wide change (a new room
        # for every class) still reaches instances that only moved.
        if change.starts_local is not None and change.starts_local != change.recurrence_local:
            fields["replacement_start_local"] = change.starts_local
        if change.duration_minutes is not None and change.duration_minutes != template.duration_minutes:
            fields["replacement_duration_minutes"] = change.duration_minutes
        if change.title and change.title.strip() != template.title:
            fields["replacement_title"] = change.title.strip()
        if change.location_text is not None and change.location_text != template.location_text:
            fields["location_text"] = change.location_text
        if change.teacher is not None and change.teacher != template.teacher:
            fields["teacher"] = change.teacher
        return fields if len(fields) > 1 else {}

    @staticmethod
    def _same(existing, wanted: dict[str, object]) -> bool:
        return (existing.action is wanted["action"]
                and existing.replacement_start_local == wanted.get("replacement_start_local")
                and existing.replacement_duration_minutes == wanted.get("replacement_duration_minutes")
                and existing.replacement_title == wanted.get("replacement_title")
                and existing.location_text == wanted.get("location_text")
                and existing.teacher == wanted.get("teacher"))

    def _reopen_series(self, template_id: str) -> None:
        self.repo.connection.execute(
            "UPDATE recurring_templates SET series_end_before_local=NULL,version=version+1,updated_at=? WHERE account_id=? AND id=?",
            (_iso(self.now), self.account_id, template_id))
        self.repo._record_change(self.repo.connection, account_id=self.account_id, entity_type="RECURRING_TEMPLATE",
                                 entity_id=template_id, action="REOPEN_RECURRING_TEMPLATE", actor=self.actor)

    def _apply_event(self, source: str, item: SourceEvent, report: ApplyReport) -> None:
        if item.ends_at <= item.starts_at:
            raise ValidationError(f"source event {item.uid} must end after it starts")
        row = self._identity(source, item.uid)
        if self._is_stale(row, item.sequence, item.updated_at):
            report.stale.append(item.uid)
            self._touch(source, item.uid)
            return
        event_id = local_id("src-event", source, item.uid) if row is None else row["local_id"]
        template_id = None
        if item.series_uid:
            series = self._identity(source, item.series_uid)
            template_id = None if series is None else series["local_id"]
        if row is None:
            self.repo.create_event(
                account_id=self.account_id, obligation_id=event_id, title=item.title.strip(),
                description=item.description, time_semantics=EventTimeSemantics.FIXED_INTERVAL,
                starts_at=item.starts_at, ends_at=item.ends_at, category=item.category, importance=Importance.NORMAL,
                attendance_policy=AttendancePolicy.REQUIRED, location_effect=LocationEffect(),
                arrival_requirement_minutes=0, actor=self.actor,
            )
            if template_id:
                self.repo.connection.execute(
                    "INSERT OR IGNORE INTO series_extra_events(account_id,event_id,template_id,created_at) VALUES (?,?,?,?)",
                    (self.account_id, event_id, template_id, _iso(self.now)))
            report.created.append(item.uid)
        else:
            current = self.repo.get_event(self.account_id, event_id)
            status = current.obligation.lifecycle_status
            differs = (current.interval.starts_at != item.starts_at or current.interval.ends_at != item.ends_at
                       or current.obligation.title != item.title.strip() or current.obligation.description != item.description
                       or current.obligation.category != item.category)
            if differs:
                self.repo.update_fixed_event(
                    account_id=self.account_id, obligation_id=event_id, expected_version=current.obligation.version,
                    starts_at=item.starts_at, ends_at=item.ends_at, title=item.title.strip(),
                    description=item.description, category=item.category, actor=self.actor)
            source_was_cancelled = bool(row["source_cancelled"])
            source_restored = row["state"] == "REMOVED" or source_was_cancelled
            reopened = (status is LifecycleStatus.CANCELLED and not item.cancelled and source_restored
                        and not bool(row["user_cancelled"]))
            if reopened:
                self._transition(event_id, "REOPEN")
        details_changed = set_event_details(
            self.repo, self.account_id, event_id, now=self.now,
            location_text=item.location_text, teacher=item.teacher, actor=self.actor,
        )
        lifecycle_changed = False
        if item.cancelled and self.repo.get_event(self.account_id, event_id).obligation.lifecycle_status is not LifecycleStatus.CANCELLED:
            self._transition(event_id, "CANCEL")
            lifecycle_changed = True
        if row is not None:
            if row["state"] == "REMOVED" or (bool(row["source_cancelled"]) and not item.cancelled):
                report.restored.append(item.uid)
            elif differs or details_changed or lifecycle_changed or bool(row["source_cancelled"]) != item.cancelled:
                report.updated.append(item.uid)
            else:
                report.unchanged.append(item.uid)
        self._remember(source, item.uid, "", kind="EVENT", local=event_id, sequence=item.sequence,
                       updated_at=item.updated_at, template_id=template_id, source_cancelled=item.cancelled)
        if row is not None:
            # A reminder the user set on this event follows its new time / state.
            self._event_reminder(event_id)

    def _event_reminder(self, event_id: str) -> None:
        from student_execution_os.reminders.events import sync_event_reminder
        sync_event_reminder(self.repo, self.account_id, event_id, self.now, by_user=False)

    def _transition(self, event_id: str, action: str) -> None:
        current = self.repo.get_event(self.account_id, event_id)
        method = {"CANCEL": self.repo.cancel_obligation, "REOPEN": self.repo.reopen_obligation}[action]
        method(account_id=self.account_id, obligation_id=event_id,
               expected_version=current.obligation.version, actor=self.actor)

    def _remove_missing(self, source: str, seen: set[tuple[str, str]], report: ApplyReport) -> None:
        rows = self.repo.connection.execute(
            "SELECT * FROM external_identities WHERE account_id=? AND source_system_id=? AND external_recurrence_id='' "
            "AND state='ACTIVE'", (self.account_id, source)).fetchall()
        for row in rows:
            if (row["external_uid"], "") in seen:
                continue
            if row["local_kind"] == "SERIES":
                template = self.recurrence.get_template(self.account_id, row["local_id"])
                zone = ZoneInfo(template.timezone_name)
                # Past classes are history; nothing happens from now on.
                boundary = self.now.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
                self.recurrence.end_series(account_id=self.account_id, template_id=template.id,
                                           before_local=boundary, actor=self.actor)
            elif row["local_kind"] == "EVENT":
                event = self.repo.get_event(self.account_id, row["local_id"])
                if event.interval.starts_at > self.now and event.obligation.lifecycle_status is not LifecycleStatus.CANCELLED:
                    self._transition(event.obligation.id, "CANCEL")
                    self._event_reminder(event.obligation.id)
            self.repo.connection.execute(
                "UPDATE external_identities SET state='REMOVED',source_cancelled=CASE WHEN local_kind='EVENT' THEN 1 "
                "ELSE source_cancelled END,last_seen_at=? WHERE account_id=? AND source_system_id=? "
                "AND external_uid=? AND external_recurrence_id=''", (_iso(self.now), self.account_id, source, row["external_uid"]))
            report.removed.append(row["external_uid"])
