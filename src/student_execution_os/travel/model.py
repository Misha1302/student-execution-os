from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from student_execution_os.domain.model import HalfOpenInterval, require_aware


class LocationContextState(StrEnum):
    KNOWN = "KNOWN"
    ASSUMED = "ASSUMED"
    UNKNOWN = "UNKNOWN"


class TravelEstimateSource(StrEnum):
    ROUTING_PROVIDER = "ROUTING_PROVIDER"
    USER_OVERRIDE = "USER_OVERRIDE"
    LEARNED = "LEARNED"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True)
class Place:
    id: str
    account_id: str
    display_name: str
    alias: str | None
    visibility_policy: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.display_name.strip():
            raise ValueError("place identity/account/display_name are required")
        if not self.visibility_policy:
            raise ValueError("visibility_policy is required")
        if self.version < 1:
            raise ValueError("place version must be >= 1")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("place coordinates require both latitude and longitude")


@dataclass(frozen=True)
class CurrentLocationContext:
    state: LocationContextState
    place_id: str | None
    recorded_at: datetime
    expires_at: datetime | None
    source: str

    def __post_init__(self) -> None:
        require_aware(self.recorded_at, "recorded_at")
        require_aware(self.expires_at, "expires_at")
        if not self.source:
            raise ValueError("current-location source is required")
        if self.state is LocationContextState.UNKNOWN:
            if self.place_id is not None:
                raise ValueError("UNKNOWN current location cannot carry place_id")
        elif not self.place_id:
            raise ValueError("KNOWN/ASSUMED current location requires place_id")
        if self.expires_at is not None and self.expires_at <= self.recorded_at:
            raise ValueError("current-location expiry must be after recorded_at")

    def effective_state_at(self, at: datetime) -> LocationContextState:
        require_aware(at, "at")
        if self.expires_at is not None and at >= self.expires_at:
            return LocationContextState.UNKNOWN
        return self.state


@dataclass(frozen=True)
class TravelEstimate:
    id: str
    account_id: str
    origin_place_id: str
    destination_place_id: str
    transport_mode: str
    expected_duration_minutes: int
    safe_duration_minutes: int
    source: TravelEstimateSource
    source_revision: str | None
    departure_time_or_bucket: str | None
    calculated_at: datetime
    expires_at: datetime | None

    def __post_init__(self) -> None:
        require_aware(self.calculated_at, "calculated_at")
        require_aware(self.expires_at, "expires_at")
        if not all(
            (
                self.id,
                self.account_id,
                self.origin_place_id,
                self.destination_place_id,
                self.transport_mode,
            )
        ):
            raise ValueError("travel estimate identity/route/mode are required")
        if self.expected_duration_minutes <= 0:
            raise ValueError("expected travel duration must be positive")
        if self.safe_duration_minutes < self.expected_duration_minutes:
            raise ValueError("safe travel duration must be >= expected duration")
        if self.expires_at is not None and self.expires_at <= self.calculated_at:
            raise ValueError("travel estimate expiry must be after calculated_at")

    def is_fresh_at(self, at: datetime) -> bool:
        require_aware(at, "at")
        return self.expires_at is None or at < self.expires_at


@dataclass(frozen=True)
class TravelTransition:
    travel_interval: HalfOpenInterval
    arrival_buffer: HalfOpenInterval | None
    origin_place_id: str
    destination_place_id: str
    travel_estimate_id: str
    target_event_id: str
    arrival_requirement_minutes: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.origin_place_id,
                self.destination_place_id,
                self.travel_estimate_id,
                self.target_event_id,
            )
        ):
            raise ValueError("travel transition identity/route/target are required")
        if self.arrival_requirement_minutes < 0:
            raise ValueError("arrival requirement cannot be negative")
        if self.arrival_buffer is not None:
            if self.travel_interval.ends_at != self.arrival_buffer.starts_at:
                raise ValueError("arrival buffer must immediately follow travel")
            duration = int(
                (
                    self.arrival_buffer.ends_at
                    - self.arrival_buffer.starts_at
                ).total_seconds()
                // 60
            )
            if duration != self.arrival_requirement_minutes:
                raise ValueError("arrival buffer duration mismatch")
        elif self.arrival_requirement_minutes != 0:
            raise ValueError("positive arrival requirement needs a buffer interval")

    @property
    def latest_safe_departure(self) -> datetime:
        return self.travel_interval.starts_at

    @property
    def hard_occupancy(self) -> tuple[HalfOpenInterval, ...]:
        if self.arrival_buffer is None:
            return (self.travel_interval,)
        return (self.travel_interval, self.arrival_buffer)


@dataclass(frozen=True)
class TravelProjection:
    transitions: tuple[TravelTransition, ...] = ()
    unknown_reasons: tuple[str, ...] = ()
    infeasible_reasons: tuple[str, ...] = ()
