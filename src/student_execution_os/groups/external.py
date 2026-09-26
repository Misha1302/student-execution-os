"""External-event identity for group annotations, independent of any particular source.

An imported event is a local Event bound to an external entity through
``source_bindings`` (account-scoped). A group annotation must name that external
event the same way for every member, so it uses:

    external_source_key      the connector's provider and scope ("google_calendar:<calendar>"),
                             or, for a source without a connector, "<kind>:<source system id>"
    external_event_uid       the source's stable id of the event or of its recurring series
    external_occurrence_key  the source's id of one occurrence of a recurring series

Identity priority (never the title): explicit binding → stable external uid +
occurrence → stable group entity id. Nothing here merges events heuristically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt


@dataclass(frozen=True)
class ExternalIdentity:
    source_key: str
    event_uid: str
    occurrence_key: str | None

    @property
    def match_key(self) -> str:
        """The external entity id a member's own imported copy carries."""
        return self.occurrence_key or self.event_uid


@dataclass(frozen=True)
class OfficialFields:
    title: str | None
    starts_at: datetime
    ends_at: datetime
    location: str | None


def source_key(kind: str, source_system_id: str, provider: str | None, scope: str | None) -> str:
    if provider and scope:
        return f"{provider}:{scope}"
    return f"{kind}:{source_system_id}"


_BOUND_SQL = (
    "SELECT sb.local_entity_id,sb.external_entity_id,sb.source_system_id,ss.kind,cs.provider,cs.scope "
    "FROM source_bindings sb JOIN source_systems ss ON ss.account_id=sb.account_id AND ss.id=sb.source_system_id "
    "LEFT JOIN connector_states cs ON cs.account_id=sb.account_id AND cs.source_system_id=sb.source_system_id "
    "WHERE sb.account_id=? AND sb.state='ACTIVE'"
)


def _series_of(repo: SQLiteCanonicalRepository, account_id: str, source_system_id: str, external_id: str) -> str | None:
    """The recurring series an occurrence belongs to, when the source reported one."""
    row = repo.connection.execute(
        "SELECT metadata_json FROM source_records WHERE account_id=? AND source_system_id=? AND external_entity_id=? "
        "ORDER BY coalesce(revision_order,0) DESC,observed_at DESC LIMIT 1",
        (account_id, source_system_id, external_id),
    ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(row["metadata_json"])
    except ValueError:
        return None
    series = metadata.get("recurring_event_id") if isinstance(metadata, dict) else None
    return str(series) if series else None


def identity_of_local_event(repo: SQLiteCanonicalRepository, account_id: str, event_id: str) -> ExternalIdentity:
    """The external identity of one of the caller's own imported events."""
    row = repo.connection.execute(_BOUND_SQL + " AND sb.local_entity_id=? ORDER BY sb.created_at DESC LIMIT 1",
                                  (account_id, event_id)).fetchone()
    if row is None:
        raise ValidationError("this event was not imported from an external calendar")
    key = source_key(row["kind"], row["source_system_id"], row["provider"], row["scope"])
    external_id = row["external_entity_id"]
    series = _series_of(repo, account_id, row["source_system_id"], external_id)
    if series and series != external_id:
        return ExternalIdentity(key, series, external_id)
    return ExternalIdentity(key, external_id, None)


def official_fields(repo: SQLiteCanonicalRepository, account_id: str, event_id: str) -> OfficialFields:
    """External-owned values of an imported event, read from the member's own copy."""
    try:
        event = repo.get_event(account_id, event_id)
    except EntityNotFound as exc:
        raise ValidationError("event not found") from exc
    location = None
    destination = event.location_effect.destination_place_id
    if destination:
        place = repo.connection.execute(
            "SELECT coalesce(alias,display_name) AS label FROM places WHERE account_id=? AND id=?", (account_id, destination)
        ).fetchone()
        location = None if place is None else place["label"]
    return OfficialFields(event.obligation.title, event.interval.starts_at, event.interval.ends_at, location)


def local_copies(repo: SQLiteCanonicalRepository, account_id: str) -> dict[tuple[str, str], str]:
    """(source key, external entity id) → the account's own local event id, for its active imports."""
    result: dict[tuple[str, str], str] = {}
    for row in repo.connection.execute(_BOUND_SQL, (account_id,)).fetchall():
        key = source_key(row["kind"], row["source_system_id"], row["provider"], row["scope"])
        result[(key, row["external_entity_id"])] = row["local_entity_id"]
    return result


def local_copy_for(copies: dict[tuple[str, str], str], binding: dict[str, Any]) -> str | None:
    match = binding["external_occurrence_key"] or binding["external_event_uid"]
    return copies.get((binding["external_source_key"], match))


def resolve_official(repo: SQLiteCanonicalRepository, binding: dict[str, Any]) -> OfficialFields:
    """Current official values: the binding member's own copy when they still have it,
    otherwise the last observed snapshot kept on the binding."""
    copies = local_copies(repo, binding["bound_by_account_id"]) if _account_exists(repo, binding["bound_by_account_id"]) else {}
    local = local_copy_for(copies, binding)
    if local is not None:
        try:
            return official_fields(repo, binding["bound_by_account_id"], local)
        except ValidationError:
            pass
    return OfficialFields(binding["official_title"], _dt(binding["official_starts_at"]), _dt(binding["official_ends_at"]),
                          binding["official_location"])


def _account_exists(repo: SQLiteCanonicalRepository, account_id: str) -> bool:
    return repo.connection.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is not None
