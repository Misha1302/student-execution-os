"""Places, the current location, manual travel times and location triggers (schema v32)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.travel import LocationContextState, SQLiteTravelRepository, TravelEstimateSource
from student_execution_os.travel.repository import clean_place_fields
from student_execution_os.travel.triggers import SQLiteLocationTriggerRepository, clean_trigger_fields

from ..primitives import APPLIED, NOOP, _ID, Outcome

from .base import CommandHandler, Handler

TRANSPORT_MODES = ("WALK", "TRANSIT", "DRIVE", "BICYCLE")


def place_payload(place) -> dict[str, Any]:
    """A place as lists, caches and operation results carry it: no exact address or
    position (those are read only on explicit request, see EventService.place_detail)."""
    return {"kind": "PLACE", "id": place.id, "display_name": place.display_name, "alias": place.alias,
            "has_address": bool(place.address), "has_coordinates": place.latitude is not None,
            "visibility_policy": place.visibility_policy, "routing_allowed": place.routing_allowed,
            "version": place.version}


def _version(payload: dict[str, Any]) -> int | None:
    value = payload.get("expected_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("expected_version must be an integer")
    return value


class PlaceCommandHandler(CommandHandler):
    """Places, the current location, manual travel times and location triggers."""

    def operations(self) -> dict[str, Handler]:
        return {
            "place.create": self.place_create,
            "place.update": self.place_update,
            "place.delete": self.place_delete,
            "location.set": self.location_set,
            "travel.estimate.set": self.travel_estimate_set,
            "location_trigger.create": self.trigger_create,
            "location_trigger.update": self.trigger_update,
            "location_trigger.fire": self.trigger_fire,
            "location_trigger.done": self.trigger_done,
            "location_trigger.cancel": self.trigger_cancel,
            "location_trigger.reopen": self.trigger_reopen,
            "location_trigger.delete": self.trigger_delete,
        }

    def _travel(self) -> SQLiteTravelRepository:
        return SQLiteTravelRepository(self.repo)

    def _triggers(self) -> SQLiteLocationTriggerRepository:
        return SQLiteLocationTriggerRepository(self.repo)

    def _phones_refresh(self) -> None:
        """The account's phones re-read the place reminders they watch (same signal as alarms)."""
        from student_execution_os.reminders.store import ReminderStore
        ReminderStore(self.repo).signal_alarm_sync(self.account_id, self.now)

    # ---- places -------------------------------------------------------------------------

    def place_create(self, place_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(place_id):
            raise ValidationError("place id must be a client-generated identifier (8-128 safe characters)")
        travel = self._travel()
        owner = travel.owner_of_place(place_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("place id is already in use")
            return Outcome(NOOP, place_payload(travel.get_place(self.account_id, place_id)), "ALREADY_EXISTS")
        fields = clean_place_fields({k: v for k, v in payload.items() if k != "assistant_batch_id"}, creating=True)
        place = travel.create_place(
            account_id=self.account_id, place_id=place_id, display_name=fields["display_name"],
            alias=fields.get("alias"), address=fields.get("address"), latitude=fields.get("latitude"),
            longitude=fields.get("longitude"), visibility_policy=fields["visibility_policy"],
            routing_allowed=fields.get("routing_allowed", False), actor=self._capture_actor(payload),
        )
        return Outcome(APPLIED, place_payload(place))

    def place_update(self, place_id: str, payload: dict[str, Any]) -> Outcome:
        fields = clean_place_fields({k: v for k, v in payload.items() if k != "expected_version"}, creating=False)
        travel = self._travel()
        before = travel.get_place(self.account_id, place_id)
        place = travel.update_place(account_id=self.account_id, place_id=place_id, fields=fields, actor=self.actor,
                                    expected_version=_version(payload))
        if {"latitude", "longitude"} & set(fields):
            self._phones_refresh()
        return Outcome(APPLIED if place.version != before.version else NOOP, place_payload(place),
                       None if place.version != before.version else "NOTHING_TO_CHANGE")

    def place_delete(self, place_id: str, payload: dict[str, Any]) -> Outcome:
        self._travel().delete_place(account_id=self.account_id, place_id=place_id, actor=self.actor)
        return Outcome(APPLIED, {"kind": "PLACE", "id": place_id, "deleted": True})

    def location_set(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        """«Я сейчас дома» — a statement with an expiry, never a permanent fact."""
        unknown = set(payload) - {"state", "place_id", "expires_in_minutes"}
        if unknown:
            raise ValidationError("location fields are not supported: " + ", ".join(sorted(unknown)))
        try:
            state = LocationContextState(payload.get("state") or "KNOWN")
        except ValueError as exc:
            raise ValidationError("state must be KNOWN, ASSUMED or UNKNOWN") from exc
        place_id = payload.get("place_id") or None
        expires = payload.get("expires_in_minutes")
        if state is not LocationContextState.UNKNOWN:
            if isinstance(expires, bool) or not isinstance(expires, int) or not 15 <= expires <= 24 * 60:
                raise ValidationError("a current location needs expires_in_minutes between 15 and 1440")
        context = self._travel().set_current_location(
            account_id=self.account_id, state=state, source="USER_MANUAL", actor=self.actor,
            place_id=None if state is LocationContextState.UNKNOWN else place_id, recorded_at=self.now,
            expires_at=None if state is LocationContextState.UNKNOWN else self.now + timedelta(minutes=expires),
        )
        return Outcome(APPLIED, {"kind": "CURRENT_LOCATION", "state": context.state.value, "place_id": context.place_id,
                                 "recorded_at": context.recorded_at.isoformat(),
                                 "expires_at": None if context.expires_at is None else context.expires_at.isoformat()})

    def travel_estimate_set(self, estimate_id: str, payload: dict[str, Any]) -> Outcome:
        """«Дом → ВШЭ: 40 минут» — the user's own estimate (USER_OVERRIDE evidence)."""
        if not _ID.match(estimate_id):
            raise ValidationError("estimate id must be a client-generated identifier")
        allowed = {"origin_place_id", "destination_place_id", "transport_mode", "expected_minutes", "safe_minutes",
                   "valid_days"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("travel estimate fields are not supported: " + ", ".join(sorted(unknown)))
        existing = self.repo.connection.execute("SELECT account_id FROM travel_estimates WHERE id=?",
                                                (estimate_id,)).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("estimate id is already in use")
            return Outcome(NOOP, {"kind": "TRAVEL_ESTIMATE", "id": estimate_id}, "ALREADY_EXISTS")
        mode = str(payload.get("transport_mode") or "TRANSIT")
        if mode not in TRANSPORT_MODES:
            raise ValidationError("transport_mode must be WALK, TRANSIT, DRIVE or BICYCLE")
        expected, safe = payload.get("expected_minutes"), payload.get("safe_minutes")
        for value in (expected, safe):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 24 * 60):
                raise ValidationError("travel minutes must be whole numbers from 1 to 1440")
        if expected is None:
            raise ValidationError("expected_minutes is required")
        safe = expected if safe is None else safe
        if safe < expected:
            raise ValidationError("safe_minutes cannot be shorter than expected_minutes")
        days = payload.get("valid_days", 30)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValidationError("valid_days must be 1-365")
        origin, destination = str(payload.get("origin_place_id") or ""), str(payload.get("destination_place_id") or "")
        if not origin or not destination or origin == destination:
            raise ValidationError("a route needs two different places")
        estimate = self._travel().add_travel_estimate(
            account_id=self.account_id, estimate_id=estimate_id, origin_place_id=origin, destination_place_id=destination,
            transport_mode=mode, expected_duration_minutes=expected, safe_duration_minutes=safe,
            source=TravelEstimateSource.USER_OVERRIDE, actor=self.actor, source_revision="user",
            calculated_at=self.now, expires_at=self.now + timedelta(days=days),
        )
        return Outcome(APPLIED, {"kind": "TRAVEL_ESTIMATE", "id": estimate.id, "origin_place_id": origin,
                                 "destination_place_id": destination, "transport_mode": mode,
                                 "expected_duration_minutes": expected, "safe_duration_minutes": safe,
                                 "source": estimate.source.value, "expires_at": estimate.expires_at.isoformat()})

    # ---- location triggers ----------------------------------------------------------------

    def trigger_create(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(trigger_id):
            raise ValidationError("trigger id must be a client-generated identifier (8-128 safe characters)")
        triggers = self._triggers()
        owner = triggers.owner_of(trigger_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("trigger id is already in use")
            return Outcome(NOOP, triggers.get(self.account_id, trigger_id), "ALREADY_EXISTS")
        fields = clean_trigger_fields({k: v for k, v in payload.items() if k != "assistant_batch_id"}, creating=True)
        created = triggers.create(account_id=self.account_id, trigger_id=trigger_id, fields=fields,
                                  actor=self._capture_actor(payload), now=self.now)
        self._phones_refresh()
        return Outcome(APPLIED, created)

    def trigger_update(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        fields = clean_trigger_fields(payload, creating=False)
        updated = self._triggers().update(self.account_id, trigger_id, fields, actor=self.actor, now=self.now)
        self._phones_refresh()
        return Outcome(APPLIED, updated)

    def trigger_fire(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        """The phone crossed the geofence (op id fixed by the device per transition)."""
        if set(payload) - {"transition", "occurred_at"}:
            raise ValidationError("location_trigger.fire accepts transition and occurred_at")
        transition = str(payload.get("transition") or "")
        if transition not in ("ENTER", "EXIT"):
            raise ValidationError("transition must be ENTER or EXIT")
        trigger, code = self._triggers().fire(self.account_id, trigger_id, transition=transition,
                                              occurred_at=self._execution_moment(payload), actor=self.actor,
                                              now=self.now)
        return Outcome(NOOP if code else APPLIED, trigger, code)

    def _closing(self, method, trigger_id: str) -> Outcome:
        trigger, code = method(self.account_id, trigger_id, actor=self.actor, now=self.now)
        if not code:
            self._phones_refresh()
        return Outcome(NOOP if code else APPLIED, trigger, code)

    def trigger_done(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        return self._closing(self._triggers().done, trigger_id)

    def trigger_cancel(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        return self._closing(self._triggers().cancel, trigger_id)

    def trigger_reopen(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        return self._closing(self._triggers().reopen, trigger_id)

    def trigger_delete(self, trigger_id: str, payload: dict[str, Any]) -> Outcome:
        self._triggers().delete(self.account_id, trigger_id, actor=self.actor, now=self.now)
        self._phones_refresh()
        return Outcome(APPLIED, {"kind": "LOCATION_TRIGGER", "id": trigger_id, "deleted": True})
