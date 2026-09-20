from __future__ import annotations

from datetime import timedelta

from student_execution_os.domain.model import (
    AttendancePolicy,
    Event,
    HalfOpenInterval,
    LifecycleStatus,
    LocationEffectKind,
)
from student_execution_os.travel.model import (
    CurrentLocationContext,
    LocationContextState,
    TravelProjection,
    TravelTransition,
)
from student_execution_os.travel.repository import SQLiteTravelRepository


class TravelProjectionBuilder:
    """Derive planning-only travel occupancy from canonical location-bearing events."""

    def __init__(self, repository: SQLiteTravelRepository) -> None:
        self.repository = repository

    def build(
        self,
        *,
        account_id: str,
        events: tuple[Event, ...],
        current_location: CurrentLocationContext,
        analysis_horizon_start,
        analysis_horizon_end,
    ) -> TravelProjection:
        state = current_location.effective_state_at(analysis_horizon_start)
        current_place = (
            current_location.place_id
            if state in (LocationContextState.KNOWN, LocationContextState.ASSUMED)
            else None
        )
        transitions: list[TravelTransition] = []
        unknown: list[str] = []
        infeasible: list[str] = []

        required = sorted(
            (
                event
                for event in events
                if event.obligation.lifecycle_status is LifecycleStatus.ACTIVE
                and event.attendance_policy is AttendancePolicy.REQUIRED
                and event.interval.ends_at > analysis_horizon_start
                and event.interval.starts_at < analysis_horizon_end
            ),
            key=lambda event: (event.interval.starts_at, event.obligation.id),
        )

        fixed_occupancy = [event.interval for event in required]

        for event in required:
            effect = event.location_effect
            required_origin: str | None = None
            resulting_place = current_place

            if effect.kind is LocationEffectKind.STAY:
                required_origin = effect.destination_place_id
                resulting_place = effect.destination_place_id
            elif effect.kind is LocationEffectKind.MOVE:
                required_origin = effect.origin_place_id
                resulting_place = effect.destination_place_id
            elif effect.kind in (LocationEffectKind.NONE, LocationEffectKind.REMOTE):
                continue

            assert required_origin is not None
            if current_place is None:
                unknown.append(
                    f"UNKNOWN_CURRENT_LOCATION_FOR_EVENT:{event.obligation.id}"
                )
                if effect.kind is LocationEffectKind.MOVE:
                    current_place = resulting_place
                continue

            if current_place != required_origin:
                estimate = self.repository.select_fresh_estimate(
                    account_id=account_id,
                    origin_place_id=current_place,
                    destination_place_id=required_origin,
                    as_of=analysis_horizon_start,
                )
                if estimate is None:
                    stale = self.repository.route_has_stale_evidence(
                        account_id=account_id,
                        origin_place_id=current_place,
                        destination_place_id=required_origin,
                        as_of=analysis_horizon_start,
                    )
                    prefix = "STALE_TRAVEL_ESTIMATE" if stale else "MISSING_TRAVEL_ESTIMATE"
                    unknown.append(
                        f"{prefix}:{current_place}:{required_origin}:{event.obligation.id}"
                    )
                else:
                    arrival = event.arrival_requirement_minutes
                    buffer_start = event.interval.starts_at - timedelta(minutes=arrival)
                    travel_start = buffer_start - timedelta(
                        minutes=estimate.safe_duration_minutes
                    )
                    travel_interval = HalfOpenInterval(travel_start, buffer_start)
                    arrival_buffer = (
                        HalfOpenInterval(buffer_start, event.interval.starts_at)
                        if arrival > 0
                        else None
                    )
                    transition = TravelTransition(
                        travel_interval=travel_interval,
                        arrival_buffer=arrival_buffer,
                        origin_place_id=current_place,
                        destination_place_id=required_origin,
                        travel_estimate_id=estimate.id,
                        target_event_id=event.obligation.id,
                        arrival_requirement_minutes=arrival,
                    )
                    if travel_start < analysis_horizon_start:
                        infeasible.append(
                            f"TRAVEL_START_BEFORE_HORIZON:{event.obligation.id}"
                        )
                    for occupancy in transition.hard_occupancy:
                        for fixed in fixed_occupancy:
                            if fixed == event.interval:
                                continue
                            if occupancy.overlaps(fixed):
                                infeasible.append(
                                    f"TRAVEL_OVERLAPS_REQUIRED_EVENT:{event.obligation.id}"
                                )
                                break
                    transitions.append(transition)

            current_place = resulting_place

        return TravelProjection(
            transitions=tuple(transitions),
            unknown_reasons=tuple(sorted(set(unknown))),
            infeasible_reasons=tuple(sorted(set(infeasible))),
        )
