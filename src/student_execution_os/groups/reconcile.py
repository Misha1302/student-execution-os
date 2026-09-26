"""Official changes of an external calendar flowing into bound group annotations.

The external source owns the time and room of a bound event. When the copy of the
member who bound it changes (the source moved the class), the annotation keeps its
id, gets the new official values in a new version, and every member's personal
consequences are recomputed exactly as for a group reschedule. Group-owned fields
(assessment kind, criticality, attendance, notes) and personal overlays are kept.
Runs in the reminder worker's tick.
"""
from __future__ import annotations

from datetime import datetime

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from . import external, messages
from .fanout import Fanout, event_critical
from .model import SharedKind
from .repository import SQLiteGroupRepository


def reconcile_external_bindings(repo: SQLiteCanonicalRepository, now: datetime) -> int:
    groups = SQLiteGroupRepository(repo)
    rows = repo.connection.execute(
        "SELECT b.* FROM external_event_bindings b JOIN shared_events e ON e.id=b.shared_event_id "
        "JOIN groups g ON g.id=b.group_id WHERE b.status='ACTIVE' AND e.status='PUBLISHED' AND g.status='ACTIVE'"
    ).fetchall()
    changed = 0
    for binding in (dict(row) for row in rows):
        official = external.resolve_official(repo, binding)
        stored = (_dt(binding["official_starts_at"]), _dt(binding["official_ends_at"]), binding["official_location"],
                  binding["official_title"])
        if stored == (official.starts_at, official.ends_at, official.location, official.title):
            continue
        with repo._tx():
            event = groups.entity(SharedKind.SHARED_EVENT, binding["shared_event_id"])
            repo.connection.execute(
                "UPDATE external_event_bindings SET official_title=?,official_starts_at=?,official_ends_at=?,"
                "official_location=?,version=version+1,updated_at=? WHERE id=?",
                (official.title, _iso(official.starts_at), _iso(official.ends_at), official.location, _iso(now),
                 binding["id"]))
            values = {"starts_at": _iso(official.starts_at), "ends_at": _iso(official.ends_at), "location": official.location}
            changes = {k: [event[k], v] for k, v in values.items() if event[k] != v}
            if not changes:
                continue
            saved = groups.update_entity(SharedKind.SHARED_EVENT, event["id"], int(event["version"]), values, now)
            groups.audit(group_id=event["group_id"], entity_kind=SharedKind.SHARED_EVENT.value, entity_id=event["id"],
                         action="EXTERNAL_CHANGE", actor=f"external:{binding['external_source_key']}"[:200], now=now,
                         previous_version=int(event["version"]), new_version=int(saved["version"]),
                         binding_id=binding["id"], changes=changes)
            fanout = Fanout(repo, groups, now)
            if event["starts_at"] != saved["starts_at"]:
                fanout.event_moved(saved, _dt(event["starts_at"]), _dt(saved["starts_at"]))
            group = groups.require_group(event["group_id"])
            critical = event_critical(saved)
            fanout.notify(group, kind=SharedKind.SHARED_EVENT, entity=saved, dedupe=f"{saved['id']}:v{saved['version']}",
                          critical=critical, exclude=None,
                          compose=lambda locale, zone, critical, s=saved, c=changes: messages.event_changed(
                              s, c, locale=locale, timezone_name=zone, critical=critical))
            changed += 1
    return changed
