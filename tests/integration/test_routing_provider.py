"""Routing provider boundary (ADR 0035): fresh routes feed the planner; failures never
become a fake FEASIBLE plan; the key and the user's place names never leave as data.

The provider HTTP is replaced by httpx.MockTransport: these tests prove the adapter
contract and the planner integration, not the live provider (see the final report).
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.sync.commands import SyncService
from student_execution_os.travel.routing import (
    RouteRefreshService,
    RoutingUnavailable,
    UnconfiguredRoutingProvider,
    YandexDistanceMatrixProvider,
    provider_from_environment,
    refresh_due_routes,
)
from student_execution_os.web.queries import UiService

T0 = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)  # 09:00 Moscow
KEY = "sk-routing-SECRET-0123456789"


def matrix(seconds: int, status: str = "OK") -> dict:
    return {"rows": [{"elements": [{"status": status, "duration": {"value": seconds}, "distance": {"value": 9000}}]}]}


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "routing.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.connection.commit()
        self.n = 0
        for op, entity, payload in [
            ("place.create", "place-home-1", {"display_name": "Дом", "address": "Секретная улица 1",
                                              "latitude": 55.7512, "longitude": 37.6184, "routing_allowed": True}),
            ("place.create", "place-hse-1", {"display_name": "Высшая школа экономики", "alias": "ВШЭ",
                                             "latitude": 55.7539, "longitude": 37.6488, "routing_allowed": True}),
            ("location.set", "", {"state": "KNOWN", "place_id": "place-home-1", "expires_in_minutes": 600}),
            ("event.create", "event-hse-class", {"title": "Семинар", "starts_at": "2026-10-06T09:00:00Z",
                                                 "ends_at": "2026-10-06T10:30:00Z",
                                                 "location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"},
                                                 "arrival_requirement_minutes": 10}),
        ]:
            self.op(op, entity, payload)

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, op_type, entity_id, payload, at=T0):
        self.n += 1
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(at)) as repo:
            repo.initialize()
            result = SyncService(repo, account_id="a", principal_id="u", now=at).apply({
                "op_id": f"op-{self.n:06d}", "type": op_type, "entity_id": entity_id, "payload": payload})
            repo.connection.commit()
        self.assertEqual(result["status"], "APPLIED", result)
        return result

    def refresh(self, provider, at=T0):
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(at)) as repo:
            repo.initialize()
            stats = RouteRefreshService(repo, provider).refresh("a", at)
            repo.connection.commit()
            return stats

    def today(self, at=T0):
        return UiService(self.db, account_id="a", principal_id="u", now=lambda: at).planning.today()

    def provider(self, recorder):
        return YandexDistanceMatrixProvider(KEY, transport=httpx.MockTransport(recorder))

    def test_scenario_6_fresh_route_feeds_the_plan(self):
        self.assertEqual(self.today()["plan"]["feasibility_status"], "UNKNOWN")
        recorder = Recorder((200, matrix(32 * 60)))
        stats = self.refresh(self.provider(recorder))
        self.assertEqual(stats["stored"], 1)
        today = self.today()
        self.assertEqual(today["plan"]["feasibility_status"], "FEASIBLE", today["plan"]["explanations"])
        transition = today["travel"]["transitions"][0]
        self.assertEqual((transition["expected_duration_minutes"], transition["safe_duration_minutes"]), (32, 44))
        self.assertEqual(transition["source"], "ROUTING_PROVIDER")
        # Only coordinates and the mode went out — no names, no address.
        sent = str(recorder.requests[0].url)
        self.assertIn("mode=transit", sent)
        self.assertIn("55.751200%2C37.618400", sent)
        self.assertNotIn("ВШЭ", sent)
        self.assertNotIn("Секретная", sent)
        with SQLiteCanonicalRepository(self.db) as repo:
            row = repo.connection.execute("SELECT source_revision,expires_at FROM travel_estimates").fetchone()
        self.assertEqual(row["source_revision"], "yandex-distancematrix-v2")
        self.assertIsNotNone(row["expires_at"])
        # Fresh evidence: nothing is asked again.
        self.assertEqual(self.refresh(self.provider(recorder))["requested"], 0)

    def test_expired_route_is_stale_not_fresh_and_is_refreshed(self):
        self.refresh(self.provider(Recorder((200, matrix(30 * 60)))))
        later = T0 + timedelta(hours=6, minutes=1)
        self.op("event.create", "event-hse-late", {"title": "Поздний семинар", "starts_at": "2026-10-06T15:00:00Z",
                                                   "ends_at": "2026-10-06T16:00:00Z",
                                                   "location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"}},
                at=later)
        self.op("location.set", "", {"state": "KNOWN", "place_id": "place-home-1", "expires_in_minutes": 600}, at=later)
        stale = self.today(later)
        self.assertEqual(stale["plan"]["feasibility_status"], "UNKNOWN")
        self.assertTrue(any(r.startswith("STALE_TRAVEL_ESTIMATE") for r in stale["travel"]["unknown_reasons"]))
        changed = Recorder((200, matrix(50 * 60)))
        self.assertEqual(self.refresh(self.provider(changed), at=later)["stored"], 1)
        fresh = self.today(later)["travel"]["transitions"][0]
        self.assertEqual(fresh["expected_duration_minutes"], 50)

    def test_timeout_and_errors_store_nothing_and_back_off(self):
        timeout = Recorder(httpx.ReadTimeout("slow"))
        stats = self.refresh(self.provider(timeout))
        self.assertEqual((stats["failed"], stats["stored"]), (1, 0))
        self.assertEqual(len(timeout.requests), 2)  # one bounded retry of a pure read
        self.assertEqual(self.today()["plan"]["feasibility_status"], "UNKNOWN")
        with SQLiteCanonicalRepository(self.db) as repo:
            state = repo.connection.execute("SELECT last_status,failures,next_attempt_at FROM route_refresh_state").fetchone()
        self.assertEqual((state["last_status"], state["failures"]), ("TIMEOUT", 1))
        # Backed off: the next minute does not hammer the provider.
        again = Recorder((200, matrix(600)))
        self.assertEqual(self.refresh(self.provider(again), at=T0 + timedelta(minutes=1))["requested"], 0)
        self.assertEqual(again.requests, [])
        for status, code in ((500, "HTTP_500"), (401, "AUTH"), (429, "RATE_LIMITED")):
            with self.assertRaises(RoutingUnavailable) as caught:
                self.provider(Recorder((status, {}))).route(self._request())
            self.assertEqual(caught.exception.reason, code)
        with self.assertRaises(RoutingUnavailable) as caught:
            self.provider(Recorder((200, matrix(0, status="FAIL")))).route(self._request())
        self.assertEqual(caught.exception.reason, "NO_ROUTE")
        with self.assertRaises(RoutingUnavailable) as caught:
            self.provider(Recorder((200, {"rows": []}))).route(self._request())
        self.assertEqual(caught.exception.reason, "BAD_RESPONSE")

    def _request(self):
        from student_execution_os.travel.routing import RouteRequest
        return RouteRequest((55.75, 37.61), (55.76, 37.64), "TRANSIT", T0)

    def test_missing_credential_means_no_provider_and_an_honest_unknown(self):
        self.assertFalse(provider_from_environment().configured)
        self.assertEqual(refresh_due_routes(self.db, UnconfiguredRoutingProvider(), T0)["requested"], 0)
        self.assertEqual(self.today()["plan"]["feasibility_status"], "UNKNOWN")
        with self.assertRaises(RoutingUnavailable):
            UnconfiguredRoutingProvider().route(self._request())

    def test_places_without_consent_are_never_sent(self):
        self.op("place.update", "place-hse-1", {"routing_allowed": False})
        recorder = Recorder((200, matrix(600)))
        stats = self.refresh(self.provider(recorder))
        self.assertEqual((stats["skipped"], recorder.requests), (1, []))

    def test_the_users_own_estimate_wins_while_valid(self):
        self.op("travel.estimate.set", "estimate-user-1", {"origin_place_id": "place-home-1",
                                                           "destination_place_id": "place-hse-1",
                                                           "expected_minutes": 40, "safe_minutes": 55})
        recorder = Recorder((200, matrix(20 * 60)))
        self.assertEqual(self.refresh(self.provider(recorder))["requested"], 0)
        self.assertEqual(self.today()["travel"]["transitions"][0]["safe_duration_minutes"], 55)

    def test_the_key_never_appears_in_logs_errors_or_the_database(self):
        captured: list[str] = []

        class Sink(logging.Handler):
            def emit(self, record):
                captured.append(self.format(record))

        sink = Sink(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(sink)
        previous = root.level
        root.setLevel(logging.DEBUG)
        try:
            self.refresh(self.provider(Recorder((200, matrix(1800)))))
            try:
                self.provider(Recorder((403, {"error": "key " + KEY}))).route(self._request())
            except RoutingUnavailable as failure:
                self.assertNotIn(KEY, str(failure))
        finally:
            root.removeHandler(sink)
            root.setLevel(previous)
        self.assertFalse(any(KEY in line for line in captured))
        with SQLiteCanonicalRepository(self.db) as repo:
            dump = json.dumps([list(row) for table in ("travel_estimates", "route_refresh_state", "audit_changes")
                               for row in repo.connection.execute(f"SELECT * FROM {table}").fetchall()])
        self.assertNotIn(KEY, dump)


if __name__ == "__main__":
    unittest.main()
