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
    TravelEstimate,
    TravelEstimateSource,
)


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
        )
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO places("
                "id,account_id,alias,display_name,address,latitude,longitude,"
                "visibility_policy,version,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,1,?,?)",
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
        )

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
        for estimate in self.list_route_estimates(
            account_id=account_id,
            origin_place_id=origin_place_id,
            destination_place_id=destination_place_id,
        ):
            if estimate.calculated_at <= as_of and estimate.is_fresh_at(as_of):
                return estimate
        return None

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
