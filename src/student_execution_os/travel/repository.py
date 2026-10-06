from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory, require_aware
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .model import (
    CurrentLocationContext,
    LocationContextState,
    Place,
    PlaceVisibility,
    TravelEstimate,
    TravelEstimateSource,
)

_PLACE_EDITABLE = {"display_name", "alias", "address", "latitude", "longitude", "visibility_policy", "routing_allowed"}


def clean_place_fields(payload: dict, *, creating: bool) -> dict:
    """Validate user-editable place fields (shared by create and update)."""
    unknown = set(payload) - _PLACE_EDITABLE
    if unknown:
        raise ValidationError("place fields are not supported: " + ", ".join(sorted(unknown)))
    fields: dict = {}
    if creating or "display_name" in payload:
        name = " ".join(str(payload.get("display_name") or "").split())
        if not name or len(name) > 120:
            raise ValidationError("a place needs a name of up to 120 characters")
        fields["display_name"] = name
    if "alias" in payload:
        alias = " ".join(str(payload.get("alias") or "").split()) or None
        if alias and len(alias) > 60:
            raise ValidationError("alias is longer than 60 characters")
        fields["alias"] = alias
    if "address" in payload:
        address = " ".join(str(payload.get("address") or "").split()) or None
        if address and len(address) > 300:
            raise ValidationError("address is longer than 300 characters")
        fields["address"] = address
    if "latitude" in payload or "longitude" in payload:
        lat, lon = payload.get("latitude"), payload.get("longitude")
        if (lat is None) != (lon is None):
            raise ValidationError("coordinates need both latitude and longitude")
        if lat is not None:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError) as exc:
                raise ValidationError("coordinates must be numbers") from exc
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValidationError("coordinates are out of range")
        fields["latitude"], fields["longitude"] = lat, lon
    if creating or "visibility_policy" in payload:
        try:
            fields["visibility_policy"] = PlaceVisibility(payload.get("visibility_policy") or PlaceVisibility.PRIVATE_ALIAS.value).value
        except ValueError as exc:
            raise ValidationError("visibility_policy must be PRIVATE_ALIAS or ASSISTANT_ADDRESS") from exc
    if "routing_allowed" in payload:
        if not isinstance(payload["routing_allowed"], bool):
            raise ValidationError("routing_allowed must be true or false")
        fields["routing_allowed"] = payload["routing_allowed"]
    return fields


class SQLiteTravelRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def create_place(
        self,
        *,
        account_id: str,
        display_name: str,
        visibility_policy: str,
        actor: ActorCategory,
        alias: str | None = None,
        address: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        place_id: str | None = None,
        routing_allowed: bool = False,
    ) -> Place:
        self.canonical._require_account(account_id)
        now = self.clock.now()
        place = Place(
            id=place_id or str(uuid4()),
            account_id=account_id,
            display_name=display_name,
            alias=alias,
            visibility_policy=visibility_policy,
            address=address,
            latitude=latitude,
            longitude=longitude,
            routing_allowed=routing_allowed,
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO places("
                "id,account_id,alias,display_name,address,latitude,longitude,"
                "visibility_policy,version,created_at,updated_at,routing_allowed"
                ") VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                (
                    place.id,
                    account_id,
                    alias,
                    display_name,
                    address,
                    latitude,
                    longitude,
                    visibility_policy,
                    _iso(now),
                    _iso(now),
                    int(routing_allowed),
                ),
            )
            self.canonical._record_change(
                conn,
                account_id=account_id,
                entity_type="PLACE",
                entity_id=place.id,
                action="CREATE_PLACE",
                actor=actor,
            )
        return place

    def get_place(self, account_id: str, place_id: str) -> Place:
        row = self.connection.execute(
            "SELECT * FROM places WHERE account_id=? AND id=?",
            (account_id, place_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("place not found")
        return Place(
            id=row["id"],
            account_id=row["account_id"],
            display_name=row["display_name"],
            alias=row["alias"],
            visibility_policy=row["visibility_policy"],
            address=row["address"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            version=int(row["version"]),
            routing_allowed=bool(row["routing_allowed"]) if "routing_allowed" in row.keys() else False,
        )

    def owner_of_place(self, place_id: str) -> str | None:
        row = self.connection.execute("SELECT account_id FROM places WHERE id=?", (place_id,)).fetchone()
        return None if row is None else row["account_id"]

    def list_places(self, account_id: str) -> list[Place]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT id FROM places WHERE account_id=? ORDER BY coalesce(alias,display_name),id", (account_id,)
        ).fetchall()
        return [self.get_place(account_id, row["id"]) for row in rows]

    def update_place(self, *, account_id: str, place_id: str, fields: dict, actor: ActorCategory,
                     expected_version: int | None = None) -> Place:
        from dataclasses import replace as _replace
        current = self.get_place(account_id, place_id)
        if expected_version is not None and expected_version != current.version:
            from student_execution_os.domain.errors import VersionConflict
            raise VersionConflict(f"expected place version {expected_version}, current {current.version}")
        if not fields:
            return current
        candidate = _replace(current, **fields)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE places SET display_name=?,alias=?,address=?,latitude=?,longitude=?,visibility_policy=?,"
                "routing_allowed=?,version=version+1,updated_at=? WHERE account_id=? AND id=?",
                (candidate.display_name, candidate.alias, candidate.address, candidate.latitude, candidate.longitude,
                 candidate.visibility_policy, int(candidate.routing_allowed), _iso(now), account_id, place_id),
            )
            if (candidate.latitude, candidate.longitude, candidate.address) != (current.latitude, current.longitude, current.address):
                # Routes computed for the old position are no longer evidence for this place.
                conn.execute(
                    "UPDATE travel_estimates SET expires_at=? WHERE account_id=? AND (origin_place_id=? OR "
                    "destination_place_id=?) AND source='ROUTING_PROVIDER' AND (expires_at IS NULL OR expires_at>?)",
                    (_iso(now), account_id, place_id, place_id, _iso(now)),
                )
            self.canonical._record_change(conn, account_id=account_id, entity_type="PLACE", entity_id=place_id,
                                          action="UPDATE_PLACE", actor=actor, payload={"fields": sorted(fields)})
        return self.get_place(account_id, place_id)

    def place_references(self, account_id: str, place_id: str) -> dict[str, int]:
        """What still points at a place (open events, their options, armed triggers)."""
        events = self.connection.execute(
            "SELECT count(*) FROM events e JOIN obligations o ON o.id=e.obligation_id WHERE o.account_id=? "
            "AND o.lifecycle_status IN ('ACTIVE','DRAFT') AND (e.origin_place_id=? OR e.destination_place_id=?)",
            (account_id, place_id, place_id)).fetchone()[0]
        options = self.connection.execute(
            "SELECT count(*) FROM event_location_options l JOIN obligations o ON o.id=l.event_id WHERE l.account_id=? "
            "AND o.lifecycle_status IN ('ACTIVE','DRAFT') AND (l.origin_place_id=? OR l.destination_place_id=?)",
            (account_id, place_id, place_id)).fetchone()[0]
        triggers = self.connection.execute(
            "SELECT count(*) FROM location_triggers WHERE account_id=? AND place_id=? AND status IN ('ARMED','FIRED')",
            (account_id, place_id)).fetchone()[0]
        return {"events": int(events), "event_options": int(options), "triggers": int(triggers)}

    def delete_place(self, *, account_id: str, place_id: str, actor: ActorCategory) -> None:
        """Delete a place nothing open depends on.

        Open events/options and armed triggers block it (the user changes those first).
        Closed events that named it keep their history but lose the place (it no longer
        exists); routes and current-location records of the place are derived context
        and go with it.
        """
        self.get_place(account_id, place_id)
        references = self.place_references(account_id, place_id)
        if any(references.values()):
            from student_execution_os.domain.errors import VersionConflict
            raise VersionConflict("PLACE_IN_USE:" + ",".join(f"{k}={v}" for k, v in references.items() if v))
        now = self.clock.now()
        with self.canonical._tx() as conn:
            closed = [row[0] for row in conn.execute(
                "SELECT e.obligation_id FROM events e JOIN obligations o ON o.id=e.obligation_id WHERE o.account_id=? "
                "AND (e.origin_place_id=? OR e.destination_place_id=?)", (account_id, place_id, place_id)).fetchall()]
            for event_id in closed:
                conn.execute("UPDATE events SET location_effect_kind='NONE',origin_place_id=NULL,destination_place_id=NULL "
                             "WHERE obligation_id=?", (event_id,))
            conn.execute("DELETE FROM event_location_options WHERE account_id=? AND (origin_place_id=? OR destination_place_id=?)",
                         (account_id, place_id, place_id))
            conn.execute("DELETE FROM location_triggers WHERE account_id=? AND place_id=?", (account_id, place_id))
            conn.execute("DELETE FROM travel_estimates WHERE account_id=? AND (origin_place_id=? OR destination_place_id=?)",
                         (account_id, place_id, place_id))
            conn.execute("DELETE FROM route_refresh_state WHERE account_id=? AND (origin_place_id=? OR destination_place_id=?)",
                         (account_id, place_id, place_id))
            conn.execute("DELETE FROM current_location_context WHERE account_id=? AND place_id=?", (account_id, place_id))
            conn.execute("DELETE FROM places WHERE account_id=? AND id=?", (account_id, place_id))
            conn.execute("INSERT OR REPLACE INTO deleted_entities(account_id,entity_kind,entity_id,deleted_at) "
                         "VALUES (?,'PLACE',?,?)", (account_id, place_id, _iso(now)))
            self.canonical._record_change(conn, account_id=account_id, entity_type="PLACE", entity_id=place_id,
                                          action="DELETE_PLACE", actor=actor,
                                          payload={"closed_events_unlinked": len(closed)})

    def set_current_location(
        self,
        *,
        account_id: str,
        state: LocationContextState,
        source: str,
        actor: ActorCategory,
        place_id: str | None = None,
        recorded_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> CurrentLocationContext:
        self.canonical._require_account(account_id)
        recorded_at = recorded_at or self.clock.now()
        context = CurrentLocationContext(
            state=state,
            place_id=place_id,
            recorded_at=recorded_at,
            expires_at=expires_at,
            source=source,
        )
        if place_id is not None:
            self.get_place(account_id, place_id)
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "INSERT INTO current_location_context("
                "account_id,state,place_id,recorded_at,expires_at,source"
                ") VALUES (?,?,?,?,?,?)",
                (
                    account_id,
                    state.value,
                    place_id,
                    _iso(recorded_at),
                    _iso(expires_at),
                    source,
                ),
            )
            self.canonical._record_change(
                conn,
                account_id=account_id,
                entity_type="CURRENT_LOCATION",
                entity_id=str(cur.lastrowid),
                action="SET_CURRENT_LOCATION",
                actor=actor,
                payload={"state": state.value, "place_id": place_id},
            )
        return context

    def current_location(self, account_id: str) -> CurrentLocationContext:
        self.canonical._require_account(account_id)
        row = self.connection.execute(
            "SELECT * FROM current_location_context WHERE account_id=? "
            "ORDER BY id DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if row is None:
            return CurrentLocationContext(
                state=LocationContextState.UNKNOWN,
                place_id=None,
                recorded_at=self.clock.now(),
                expires_at=None,
                source="NO_CONTEXT",
            )
        return CurrentLocationContext(
            state=LocationContextState(row["state"]),
            place_id=row["place_id"],
            recorded_at=_dt(row["recorded_at"]),
            expires_at=_dt(row["expires_at"]),
            source=row["source"],
        )

    def add_travel_estimate(
        self,
        *,
        account_id: str,
        origin_place_id: str,
        destination_place_id: str,
        transport_mode: str,
        expected_duration_minutes: int,
        safe_duration_minutes: int,
        source: TravelEstimateSource,
        actor: ActorCategory,
        source_revision: str | None = None,
        departure_time_or_bucket: str | None = None,
        calculated_at: datetime | None = None,
        expires_at: datetime | None = None,
        estimate_id: str | None = None,
    ) -> TravelEstimate:
        self.canonical._require_account(account_id)
        self.get_place(account_id, origin_place_id)
        self.get_place(account_id, destination_place_id)
        calculated_at = calculated_at or self.clock.now()
        estimate = TravelEstimate(
            id=estimate_id or str(uuid4()),
            account_id=account_id,
            origin_place_id=origin_place_id,
            destination_place_id=destination_place_id,
            transport_mode=transport_mode,
            expected_duration_minutes=expected_duration_minutes,
            safe_duration_minutes=safe_duration_minutes,
            source=source,
            source_revision=source_revision,
            departure_time_or_bucket=departure_time_or_bucket,
            calculated_at=calculated_at,
            expires_at=expires_at,
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO travel_estimates("
                "id,account_id,origin_place_id,destination_place_id,"
                "departure_time_or_bucket,transport_mode,expected_duration_minutes,"
                "safe_duration_minutes,source,source_revision,calculated_at,expires_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    estimate.id,
                    account_id,
                    origin_place_id,
                    destination_place_id,
                    departure_time_or_bucket,
                    transport_mode,
                    expected_duration_minutes,
                    safe_duration_minutes,
                    source.value,
                    source_revision,
                    _iso(calculated_at),
                    _iso(expires_at),
                ),
            )
            self.canonical._record_change(
                conn,
                account_id=account_id,
                entity_type="TRAVEL_ESTIMATE",
                entity_id=estimate.id,
                action="ADD_TRAVEL_ESTIMATE",
                actor=actor,
                payload={
                    "origin_place_id": origin_place_id,
                    "destination_place_id": destination_place_id,
                    "source": source.value,
                },
            )
        return estimate

    def list_route_estimates(
        self,
        *,
        account_id: str,
        origin_place_id: str,
        destination_place_id: str,
    ) -> list[TravelEstimate]:
        self.canonical._require_account(account_id)
        rows = self.connection.execute(
            "SELECT * FROM travel_estimates "
            "WHERE account_id=? AND origin_place_id=? AND destination_place_id=? "
            "ORDER BY calculated_at DESC,id DESC",
            (account_id, origin_place_id, destination_place_id),
        ).fetchall()
        return [self._estimate_from_row(row) for row in rows]

    def select_fresh_estimate(
        self,
        *,
        account_id: str,
        origin_place_id: str,
        destination_place_id: str,
        as_of: datetime,
    ) -> TravelEstimate | None:
        require_aware(as_of, "as_of")
        fresh = [
            estimate for estimate in self.list_route_estimates(
                account_id=account_id,
                origin_place_id=origin_place_id,
                destination_place_id=destination_place_id,
            )
            if estimate.calculated_at <= as_of and estimate.is_fresh_at(as_of)
        ]
        # The user's own estimate wins while it is valid (they know their route); otherwise
        # the newest fresh evidence. Stale evidence is never used as fresh.
        for estimate in fresh:
            if estimate.source is TravelEstimateSource.USER_OVERRIDE:
                return estimate
        return fresh[0] if fresh else None

    def route_has_stale_evidence(
        self,
        *,
        account_id: str,
        origin_place_id: str,
        destination_place_id: str,
        as_of: datetime,
    ) -> bool:
        require_aware(as_of, "as_of")
        return any(
            estimate.calculated_at <= as_of and not estimate.is_fresh_at(as_of)
            for estimate in self.list_route_estimates(
                account_id=account_id,
                origin_place_id=origin_place_id,
                destination_place_id=destination_place_id,
            )
        )

    @staticmethod
    def _estimate_from_row(row) -> TravelEstimate:
        return TravelEstimate(
            id=row["id"],
            account_id=row["account_id"],
            origin_place_id=row["origin_place_id"],
            destination_place_id=row["destination_place_id"],
            transport_mode=row["transport_mode"],
            expected_duration_minutes=int(row["expected_duration_minutes"]),
            safe_duration_minutes=int(row["safe_duration_minutes"]),
            source=TravelEstimateSource(row["source"]),
            source_revision=row["source_revision"],
            departure_time_or_bucket=row["departure_time_or_bucket"],
            calculated_at=_dt(row["calculated_at"]),
            expires_at=_dt(row["expires_at"]),
        )
