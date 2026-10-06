"""Routing provider boundary: fresh travel times as TravelEstimate evidence (ADR 0035).

The planner never calls a provider. A worker asks the configured provider for the
routes the next day and a half needs, and stores the answer as a ROUTING_PROVIDER
``TravelEstimate`` (source revision, calculated_at, expiry, expected and safe
duration). A failing, slow or unconfigured provider stores nothing: the travel
projection then reports MISSING/STALE and the plan stays UNKNOWN instead of
pretending. The user's own estimate keeps priority while it is valid.

Privacy: only places whose owner allowed routing (``routing_allowed``) and that have
a map point are ever sent, and only their coordinates; never names or addresses.
Secrets: the API key is read from a deployment file (``SEOS_ROUTING_API_KEY_FILE``);
it never enters SQLite, a client, an error message or a log line.

Default adapter: Yandex Routing "Distance Matrix" (``api.routing.yandex.net``) —
public transport, driving and walking times for Russian cities with a commercial
SLA; Google Maps Platform cannot be billed for Russian accounts. The domain does not
depend on its DTOs: any provider implementing ``RoutingProvider`` can replace it.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .model import TravelEstimateSource

log = logging.getLogger("student_execution_os.routing")
# httpx logs full request URLs at INFO; the provider key travels in the query string.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(max(logging.getLogger(_name).level, logging.WARNING))

TRANSPORT_MODES = ("TRANSIT", "DRIVE", "WALK", "BICYCLE")
# Safe duration policy (explicit, not UI magic): variance grows with traffic/transfers.
SAFETY = {"TRANSIT": (1.2, 5), "DRIVE": (1.3, 5), "WALK": (1.1, 2), "BICYCLE": (1.15, 3)}
ESTIMATE_TTL = timedelta(hours=6)
LOOKAHEAD = timedelta(hours=36)
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_REQUESTS_PER_PASS = 20
BACKOFF = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6))


class RoutingUnavailable(Exception):
    """A route could not be obtained. ``reason`` is a stable code, safe to store/log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RouteRequest:
    origin: tuple[float, float]
    destination: tuple[float, float]
    mode: str
    departure_at: datetime


@dataclass(frozen=True)
class RouteResult:
    expected_minutes: int
    revision: str


class RoutingProvider(Protocol):
    name: str
    configured: bool

    def route(self, request: RouteRequest) -> RouteResult: ...


class UnconfiguredRoutingProvider:
    name = "none"
    configured = False

    def route(self, request: RouteRequest) -> RouteResult:
        raise RoutingUnavailable("NOT_CONFIGURED")


class YandexDistanceMatrixProvider:
    """Adapter for https://api.routing.yandex.net/v2/distancematrix (one origin, one destination)."""

    name = "yandex-distancematrix-v2"
    configured = True
    URL = "https://api.routing.yandex.net/v2/distancematrix"
    MODES = {"TRANSIT": "transit", "DRIVE": "driving", "WALK": "walking"}

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None, attempts: int = 2) -> None:
        if not api_key:
            raise ValueError("routing API key is required")
        self._key = api_key
        self._transport = transport
        self._attempts = max(1, attempts)

    def route(self, request: RouteRequest) -> RouteResult:
        mode = self.MODES.get(request.mode)
        if mode is None:
            raise RoutingUnavailable("MODE_UNSUPPORTED")
        params = {
            "origins": f"{request.origin[0]:.6f},{request.origin[1]:.6f}",
            "destinations": f"{request.destination[0]:.6f},{request.destination[1]:.6f}",
            "mode": mode,
            "departure_time": str(int(request.departure_at.timestamp())),
            "apikey": self._key,
        }
        timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        last = "NETWORK"
        # A GET of a route is a pure read: repeating it after a timeout/5xx is safe. Bounded.
        for _ in range(self._attempts):
            try:
                with httpx.Client(timeout=timeout, transport=self._transport, trust_env=False) as client:
                    response = client.get(self.URL, params=params)
            except httpx.TimeoutException:
                last = "TIMEOUT"
                continue
            except httpx.HTTPError:
                last = "NETWORK"
                continue
            if response.status_code in (401, 403):
                raise RoutingUnavailable("AUTH")
            if response.status_code == 429:
                raise RoutingUnavailable("RATE_LIMITED")
            if response.status_code >= 500:
                last = f"HTTP_{response.status_code}"
                continue
            if response.status_code != 200:
                raise RoutingUnavailable(f"HTTP_{response.status_code}")
            try:
                element = response.json()["rows"][0]["elements"][0]
            except (ValueError, KeyError, IndexError, TypeError):
                raise RoutingUnavailable("BAD_RESPONSE") from None
            if element.get("status") != "OK":
                raise RoutingUnavailable("NO_ROUTE")
            try:
                seconds = float(element["duration"]["value"])
            except (KeyError, TypeError, ValueError):
                raise RoutingUnavailable("BAD_RESPONSE") from None
            if not 0 < seconds < 24 * 3600:
                raise RoutingUnavailable("BAD_RESPONSE")
            return RouteResult(expected_minutes=max(1, math.ceil(seconds / 60)), revision=self.name)
        raise RoutingUnavailable(last)


def provider_from_environment() -> RoutingProvider:
    """SEOS_ROUTING_PROVIDER=yandex + SEOS_ROUTING_API_KEY_FILE=/run/secrets/... (deployment only)."""
    name = os.environ.get("SEOS_ROUTING_PROVIDER", "").strip().lower()
    key_file = os.environ.get("SEOS_ROUTING_API_KEY_FILE", "").strip()
    if name != "yandex" or not key_file:
        return UnconfiguredRoutingProvider()
    try:
        key = Path(key_file).read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("routing key file is not readable; routing disabled")
        return UnconfiguredRoutingProvider()
    return YandexDistanceMatrixProvider(key) if key else UnconfiguredRoutingProvider()


def safe_minutes(expected: int, mode: str) -> int:
    factor, pad = SAFETY.get(mode, SAFETY["TRANSIT"])
    return max(expected, math.ceil(expected * factor) + pad)


@dataclass(frozen=True)
class RouteNeed:
    origin_place_id: str
    destination_place_id: str
    departure_at: datetime


class RouteRefreshService:
    """Asks the provider for the routes upcoming located events need (bounded, backed off)."""

    def __init__(self, repo: SQLiteCanonicalRepository, provider: RoutingProvider, *, default_mode: str = "TRANSIT") -> None:
        self.repo = repo
        self.provider = provider
        self.default_mode = default_mode if default_mode in TRANSPORT_MODES else "TRANSIT"

    def needs(self, account_id: str, now: datetime) -> list[RouteNeed]:
        """Origin → destination pairs the travel projection will look up, as it orders them."""
        from student_execution_os.travel.repository import SQLiteTravelRepository
        from student_execution_os.travel.model import LocationContextState
        travel = SQLiteTravelRepository(self.repo)
        current = travel.current_location(account_id)
        place = current.place_id if current.effective_state_at(now) in (LocationContextState.KNOWN,
                                                                         LocationContextState.ASSUMED) else None
        rows = self.repo.connection.execute(
            "SELECT e.starts_at,e.location_effect_kind,e.origin_place_id,e.destination_place_id FROM events e "
            "JOIN obligations o ON o.id=e.obligation_id WHERE o.account_id=? AND o.lifecycle_status='ACTIVE' "
            "AND e.attendance_policy='REQUIRED' AND e.ends_at>? AND e.starts_at<? ORDER BY e.starts_at,o.id",
            (account_id, _iso(now), _iso(now + LOOKAHEAD))).fetchall()
        out: list[RouteNeed] = []
        for row in rows:
            kind = row["location_effect_kind"]
            if kind not in ("STAY", "MOVE"):
                continue
            required = row["destination_place_id"] if kind == "STAY" else row["origin_place_id"]
            if place is not None and required and place != required:
                out.append(RouteNeed(place, required, _dt(row["starts_at"]) - timedelta(hours=1)))
            place = row["destination_place_id"]
        unique: dict[tuple[str, str], RouteNeed] = {}
        for need in out:
            unique.setdefault((need.origin_place_id, need.destination_place_id), need)
        return list(unique.values())

    def _mode(self, account_id: str, origin: str, destination: str) -> str:
        row = self.repo.connection.execute(
            "SELECT transport_mode FROM travel_estimates WHERE account_id=? AND origin_place_id=? AND destination_place_id=? "
            "AND source='USER_OVERRIDE' ORDER BY calculated_at DESC LIMIT 1", (account_id, origin, destination)).fetchone()
        return row["transport_mode"] if row and row["transport_mode"] in TRANSPORT_MODES else self.default_mode

    def refresh(self, account_id: str, now: datetime, *, budget: int = MAX_REQUESTS_PER_PASS) -> dict[str, int]:
        from student_execution_os.travel.repository import SQLiteTravelRepository
        stats = {"requested": 0, "stored": 0, "skipped": 0, "failed": 0}
        travel = SQLiteTravelRepository(self.repo)
        for need in self.needs(account_id, now):
            if stats["requested"] >= budget:
                break
            if travel.select_fresh_estimate(account_id=account_id, origin_place_id=need.origin_place_id,
                                            destination_place_id=need.destination_place_id, as_of=now) is not None:
                continue  # fresh evidence (the user's own, or a recent provider answer)
            origin = travel.get_place(account_id, need.origin_place_id)
            destination = travel.get_place(account_id, need.destination_place_id)
            if not (origin.routing_allowed and destination.routing_allowed) or origin.latitude is None \
                    or destination.latitude is None:
                stats["skipped"] += 1  # no consent or no point: honestly missing, never guessed
                continue
            mode = self._mode(account_id, origin.id, destination.id)
            state = self.repo.connection.execute(
                "SELECT failures,next_attempt_at FROM route_refresh_state WHERE account_id=? AND origin_place_id=? "
                "AND destination_place_id=? AND transport_mode=?", (account_id, origin.id, destination.id, mode)).fetchone()
            if state is not None and _dt(state["next_attempt_at"]) > now:
                continue
            stats["requested"] += 1
            try:
                result = self.provider.route(RouteRequest((origin.latitude, origin.longitude),
                                                          (destination.latitude, destination.longitude), mode,
                                                          max(now, need.departure_at)))
            except RoutingUnavailable as failure:
                failures = (int(state["failures"]) if state else 0) + 1
                self._record(account_id, origin.id, destination.id, mode, now, failure.reason, failures,
                             now + BACKOFF[min(failures, len(BACKOFF)) - 1])
                stats["failed"] += 1
                continue
            expires = min(now + ESTIMATE_TTL, need.departure_at + timedelta(hours=2))
            if expires <= now:
                expires = now + timedelta(minutes=30)
            travel.add_travel_estimate(
                account_id=account_id, origin_place_id=origin.id, destination_place_id=destination.id,
                transport_mode=mode, expected_duration_minutes=result.expected_minutes,
                safe_duration_minutes=safe_minutes(result.expected_minutes, mode),
                source=TravelEstimateSource.ROUTING_PROVIDER, actor=ActorCategory.SYSTEM,
                source_revision=result.revision,
                departure_time_or_bucket=need.departure_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00Z"),
                calculated_at=now, expires_at=expires,
            )
            self._record(account_id, origin.id, destination.id, mode, now, "OK", 0, now + ESTIMATE_TTL)
            stats["stored"] += 1
        return stats

    def _record(self, account_id: str, origin: str, destination: str, mode: str, now: datetime, status: str,
                failures: int, next_attempt: datetime) -> None:
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO route_refresh_state(account_id,origin_place_id,destination_place_id,transport_mode,"
                "last_attempt_at,last_status,failures,next_attempt_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,origin_place_id,destination_place_id,transport_mode) DO UPDATE SET "
                "last_attempt_at=excluded.last_attempt_at,last_status=excluded.last_status,failures=excluded.failures,"
                "next_attempt_at=excluded.next_attempt_at",
                (account_id, origin, destination, mode, _iso(now), status[:40], failures, _iso(next_attempt)))


def refresh_due_routes(database: str, provider: RoutingProvider, now: datetime) -> dict[str, int]:
    """Worker pass over every account (no-op without a configured provider)."""
    from student_execution_os.domain.clock import FrozenClock
    totals = {"requested": 0, "stored": 0, "skipped": 0, "failed": 0}
    if not provider.configured:
        return totals
    with SQLiteCanonicalRepository(database, clock=FrozenClock(now)) as repo:
        repo.initialize()
        accounts = [row[0] for row in repo.connection.execute("SELECT id FROM accounts ORDER BY id")]
        for account_id in accounts:
            try:
                stats = RouteRefreshService(repo, provider, default_mode=os.environ.get(
                    "SEOS_ROUTING_DEFAULT_MODE", "TRANSIT")).refresh(account_id, now)
            except Exception:
                log.exception("route refresh failed for an account")
                continue
            for key, value in stats.items():
                totals[key] += value
    return totals
