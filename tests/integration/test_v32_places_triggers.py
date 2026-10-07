"""Schema v32: places as a product surface, the planner's travel input, location triggers.

Through the real sync boundary and the HTTP read models with frozen clocks.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.agent.assistant import SQLiteAssistantService
from student_execution_os.agent.model import AuthenticatedPrincipal
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.sync.commands import SyncService
from student_execution_os.web.queries import UiService
from tests.rollback_chain import roll_back_newer_than

T0 = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)  # 09:00 in Moscow
MIGRATIONS = Path("src/student_execution_os/persistence/migrations")


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v32.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_account("b")
            repo.connection.commit()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, op_type, entity_id, payload=None, *, at=T0, account="a"):
        self.n += 1
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(at)) as repo:
            repo.initialize()
            result = SyncService(repo, account_id=account, principal_id=f"u-{account}", now=at).apply({
                "op_id": f"op-{self.n:06d}", "type": op_type, "entity_id": entity_id, "payload": payload or {}})
            repo.connection.commit()
            return result

    def ok(self, op_type, entity_id, payload=None, **kw):
        result = self.op(op_type, entity_id, payload, **kw)
        self.assertEqual(result["status"], "APPLIED", result)
        return result["entity"]

    def ui(self, account="a", at=T0):
        return UiService(self.db, account_id=account, principal_id=f"u-{account}", now=lambda: at)

    def places(self):
        self.ok("place.create", "place-home-1", {"display_name": "Дом", "address": "Секретная улица 1",
                                                  "latitude": 55.7512, "longitude": 37.6184})
        self.ok("place.create", "place-hse-1", {"display_name": "Высшая школа экономики", "alias": "ВШЭ",
                                                 "address": "Покровский бульвар 11", "latitude": 55.7539,
                                                 "longitude": 37.6488})


class PlacesTests(Harness):
    def test_crud_rename_and_privacy_redaction(self):
        self.places()
        created = self.op("place.create", "place-gym-001", {"display_name": "Спортзал"})["entity"]
        self.assertEqual((created["has_address"], created["has_coordinates"]), (False, False))
        self.assertNotIn("address", created)
        renamed = self.ok("place.update", "place-gym-001", {"display_name": "Зал", "alias": "Качалка"})
        self.assertEqual((renamed["display_name"], renamed["alias"], renamed["version"]), ("Зал", "Качалка", 2))
        listing = self.ui().events.places()
        serialized = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn("Секретная улица", serialized)
        self.assertNotIn("55.75", serialized)
        self.assertTrue(next(p for p in listing["places"] if p["id"] == "place-home-1")["has_coordinates"])
        # The owner's explicit edit view does show their own data.
        detail = self.ui().events.place_detail("place-home-1")
        self.assertEqual(detail["address"], "Секретная улица 1")
        # Sync results do not echo exact data either.
        result = self.op("place.update", "place-home-1", {"address": "Другая 2"})
        self.assertNotIn("Другая", json.dumps(result, ensure_ascii=False))
        bad = self.op("place.update", "place-home-1", {"latitude": 91, "longitude": 0})
        self.assertEqual(bad["status"], "REJECTED")

    def test_cross_account_places_are_invisible_and_unusable(self):
        self.places()
        self.assertEqual(self.op("place.update", "place-home-1", {"display_name": "x"}, account="b")["code"], "NOT_FOUND")
        stolen = self.op("event.create", "event-b-000001", {
            "title": "Чужое место", "starts_at": "2026-10-07T07:00:00Z", "ends_at": "2026-10-07T08:00:00Z",
            "location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"}}, account="b")
        self.assertEqual(stolen["status"], "REJECTED")
        self.assertEqual(self.op("place.create", "place-home-1", {"display_name": "x"}, account="b")["status"], "REJECTED")
        trigger = self.op("location_trigger.create", "trigger-b-0001", {
            "place_id": "place-home-1", "transition": "ENTER", "title": "x"}, account="b")
        self.assertEqual(trigger["status"], "REJECTED")
        self.assertEqual(self.ui("b").events.places()["places"], [])

    def test_delete_refuses_while_an_open_event_or_trigger_uses_the_place(self):
        self.places()
        self.ok("event.create", "event-lecture-1", {
            "title": "Лекция", "starts_at": "2026-10-07T07:00:00Z", "ends_at": "2026-10-07T08:30:00Z",
            "location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"}})
        blocked = self.op("place.delete", "place-hse-1")
        self.assertEqual(blocked["status"], "CONFLICT")
        self.assertIn("PLACE_IN_USE", blocked["message"])
        self.ok("event.cancel", "event-lecture-1")
        self.ok("location_trigger.create", "trigger-hse-01", {"place_id": "place-hse-1", "transition": "EXIT",
                                                             "title": "Написать Саше"})
        self.assertEqual(self.op("place.delete", "place-hse-1")["status"], "CONFLICT")
        self.ok("location_trigger.cancel", "trigger-hse-01")
        self.ok("place.delete", "place-hse-1")
        # The cancelled event keeps its history but no longer names a deleted place.
        with sqlite3.connect(self.db) as conn:
            kind = conn.execute("SELECT location_effect_kind FROM events WHERE obligation_id='event-lecture-1'").fetchone()[0]
        self.assertEqual(kind, "NONE")
        late = self.op("place.update", "place-hse-1", {"display_name": "x"})
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))

    def test_scenario_4_event_at_hse_uses_travel_input_and_unknown_without_it(self):
        self.places()
        self.ok("location.set", "", {"state": "KNOWN", "place_id": "place-home-1", "expires_in_minutes": 240})
        self.ok("event.create", "event-hse-class", {
            "title": "Семинар", "starts_at": "2026-10-06T09:00:00Z", "ends_at": "2026-10-06T10:30:00Z"})
        self.ok("event.update", "event-hse-class", {"location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"},
                                                    "arrival_requirement_minutes": 10})
        today = self.ui().planning.today()
        self.assertEqual(today["plan"]["feasibility_status"], "UNKNOWN")
        self.assertTrue(any("MISSING_TRAVEL_ESTIMATE" in reason for reason in today["travel"]["unknown_reasons"]))
        self.ok("travel.estimate.set", "estimate-home-hse", {"origin_place_id": "place-home-1",
                                                             "destination_place_id": "place-hse-1",
                                                             "transport_mode": "TRANSIT", "expected_minutes": 35,
                                                             "safe_minutes": 45})
        today = self.ui().planning.today()
        self.assertEqual(today["plan"]["feasibility_status"], "FEASIBLE", today["plan"]["explanations"])
        transition = today["travel"]["transitions"][0]
        self.assertEqual((transition["origin"], transition["destination"], transition["safe_duration_minutes"]),
                         ("Дом", "ВШЭ", 45))
        # Leave by 11:05 Moscow: 12:00 start − 10 min arrival − 45 min safe travel.
        self.assertEqual(transition["latest_safe_departure"], "2026-10-06T08:05:00+00:00")
        # An expired current location is unknown again: no plan pretends to know the origin.
        self.ok("event.create", "event-hse-evening", {
            "title": "Вечерний семинар", "starts_at": "2026-10-06T15:00:00Z", "ends_at": "2026-10-06T16:30:00Z",
            "location_effect": {"kind": "STAY", "destination_place_id": "place-hse-1"}})
        later = self.ui(at=T0 + timedelta(hours=5)).planning.today()
        self.assertEqual(later["plan"]["feasibility_status"], "UNKNOWN")
        self.assertTrue(any("UNKNOWN_CURRENT_LOCATION" in reason for reason in later["travel"]["unknown_reasons"]))

    def test_current_location_requires_expiry_and_can_be_unknown(self):
        self.places()
        bad = self.op("location.set", "", {"state": "KNOWN", "place_id": "place-home-1"})
        self.assertEqual(bad["status"], "REJECTED")
        self.ok("location.set", "", {"state": "UNKNOWN"})
        self.assertEqual(self.ui().events.places()["current_location"]["state"], "UNKNOWN")


class TriggerTests(Harness):
    def test_enter_fire_duplicate_done_and_cancel_before_fire(self):
        self.places()
        trigger = self.ok("location_trigger.create", "trigger-home-1", {
            "place_id": "place-home-1", "transition": "ENTER", "title": "Разобрать вещи"})
        self.assertEqual((trigger["status"], trigger["needs_coordinates"]), ("ARMED", False))
        armed = self.ui().events.armed_location_triggers()["triggers"]
        self.assertEqual(armed[0]["latitude"], 55.7512)  # the owner's phone needs the position
        wrong = self.op("location_trigger.fire", "trigger-home-1", {"transition": "EXIT"})
        self.assertEqual((wrong["status"], wrong["code"]), ("NOOP", "OTHER_TRANSITION"))
        fired = self.ok("location_trigger.fire", "trigger-home-1", {"transition": "ENTER",
                                                                    "occurred_at": "2026-10-06T15:02:00Z"},
                        at=T0 + timedelta(hours=9, minutes=3))
        self.assertEqual((fired["status"], fired["fire_count"]), ("FIRED", 1))
        again = self.op("location_trigger.fire", "trigger-home-1", {"transition": "ENTER",
                                                                    "occurred_at": "2026-10-06T15:02:30Z"},
                        at=T0 + timedelta(hours=9, minutes=4))
        self.assertEqual((again["status"], again["code"]), ("NOOP", "ALREADY_FIRED"))
        self.assertEqual(self.ui().events.armed_location_triggers()["triggers"], [])
        self.assertEqual(self.ok("location_trigger.done", "trigger-home-1")["status"], "DONE")
        self.ok("location_trigger.create", "trigger-hse-01", {"place_id": "place-hse-1", "transition": "EXIT",
                                                             "title": "Написать Саше"})
        self.ok("location_trigger.cancel", "trigger-hse-01")
        late = self.op("location_trigger.fire", "trigger-hse-01", {"transition": "EXIT"})
        self.assertEqual((late["status"], late["code"]), ("NOOP", "TRIGGER_CLOSED"))

    def test_repeating_trigger_has_a_cooldown(self):
        self.places()
        self.ok("location_trigger.create", "trigger-gym-01", {"place_id": "place-hse-1", "transition": "ENTER",
                                                             "title": "Сдать пропуск", "repeat": True})
        first = self.ok("location_trigger.fire", "trigger-gym-01", {"transition": "ENTER",
                                                                    "occurred_at": "2026-10-06T06:00:00Z"})
        self.assertEqual((first["status"], first["fire_count"]), ("ARMED", 1))
        jitter = self.op("location_trigger.fire", "trigger-gym-01", {"transition": "ENTER",
                                                                     "occurred_at": "2026-10-06T06:05:00Z"})
        self.assertEqual(jitter["code"], "DUPLICATE_TRANSITION")
        next_day = self.ok("location_trigger.fire", "trigger-gym-01", {"transition": "ENTER",
                                                                       "occurred_at": "2026-10-07T06:00:00Z"},
                           at=T0 + timedelta(days=1, hours=1))
        self.assertEqual(next_day["fire_count"], 2)

    def test_trigger_on_a_place_without_position_is_flagged(self):
        self.ok("place.create", "place-shop-01", {"display_name": "Магазин"})
        trigger = self.ok("location_trigger.create", "trigger-shop-1", {"place_id": "place-shop-01",
                                                                        "transition": "ENTER", "title": "Молоко"})
        self.assertTrue(trigger["needs_coordinates"])
        self.assertEqual(self.ui().events.armed_location_triggers()["triggers"], [])


class AssistantPlaceTests(Harness):
    def service(self, repo):
        return SQLiteAssistantService(repo, AuthenticatedPrincipal("a", "u-a", "web"))

    def test_add_place_and_home_trigger_by_words_without_coordinates_in_context(self):
        self.places()
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            service = self.service(repo)
            context = service._context({"timezone": "Europe/Moscow"})
            serialized = json.dumps(context["places"], ensure_ascii=False)
            self.assertIn("ВШЭ", serialized)
            self.assertNotIn("55.75", serialized)
            self.assertNotIn("Покровский", serialized)  # PRIVATE_ALIAS by default
            proposal = service.interpret("Добавь место Спортзал", {"timezone": "Europe/Moscow"})
            self.assertEqual(proposal["actions"][0]["command"], "CREATE_PLACE")
            service.apply({"batch_id": proposal["batch_id"], "action_ids": [proposal["actions"][0]["id"]],
                           "idempotency_key": "place-1"})
            proposal = service.interpret("Когда приду домой, напомни разобрать вещи", {"timezone": "Europe/Moscow"})
            action = proposal["actions"][0]
            self.assertEqual((action["command"], action["payload"]["place_id"], action["payload"]["transition"]),
                             ("CREATE_LOCATION_TRIGGER", "place-home-1", "ENTER"))
            service.apply({"batch_id": proposal["batch_id"], "action_ids": [action["id"]], "idempotency_key": "trig-1"})
            unknown = service.interpret("Когда буду у магазина, напомни купить молоко", {"timezone": "Europe/Moscow"})
            self.assertIn("place_id", unknown["actions"][0]["unresolved_fields"])
            repo.connection.commit()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM places WHERE display_name='Спортзал'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT title,transition FROM location_triggers").fetchone(),
                             ("Разобрать вещи", "ENTER"))

    def test_model_cannot_reference_an_invented_place(self):
        from student_execution_os.agent.assistant import validate_proposal
        from student_execution_os.domain.errors import ValidationError
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            with self.assertRaises(ValidationError):
                validate_proposal({"command": "CREATE_LOCATION_TRIGGER", "payload": {
                    "place_id": "place-invented", "transition": "ENTER", "title": "x"}, "confidence": 0.9,
                    "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}, repo, "a")
            with self.assertRaises(ValidationError):
                validate_proposal({"command": "CREATE_PLACE", "payload": {
                    "display_name": "Дом", "latitude": 55.7}, "confidence": 0.9,
                    "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}, repo, "a")


class MigrationTests(Harness):
    def test_v31_database_upgrades_rolls_back_and_lifecycle_covers_v32(self):
        database = Path(self.tmp.name) / "up.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            for version in range(1, 32):
                connection.executescript(next(MIGRATIONS.glob(f"{version:03d}_*.sql")).read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, T0.isoformat()))
            connection.execute("INSERT INTO accounts(id) VALUES ('old')")
            connection.execute("INSERT INTO places(id,account_id,display_name,visibility_policy,created_at,updated_at) "
                               "VALUES ('p-old','old','Дом','PRIVATE_ALIAS',?,?)", (T0.isoformat(), T0.isoformat()))
        with SQLiteCanonicalRepository(database, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            row = repo.connection.execute("SELECT routing_allowed FROM places WHERE id='p-old'").fetchone()
            self.assertEqual(row[0], 0)
            SyncService(repo, account_id="old", principal_id="u", now=T0).apply({
                "op_id": "op-mig-0001", "type": "location_trigger.create", "entity_id": "trigger-old-1",
                "payload": {"place_id": "p-old", "transition": "ENTER", "title": "Разобрать вещи"}})
            repo.connection.commit()
        export = SQLiteDataLifecycle(database).export_account("old").tables
        self.assertEqual(len(export["location_triggers"]), 1)
        with sqlite3.connect(database) as conn:
            roll_back_newer_than(conn, 31)
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 31)
            self.assertEqual(conn.execute("SELECT display_name FROM places WHERE id='p-old'").fetchone()[0], "Дом")
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='location_triggers'").fetchone())
        with SQLiteCanonicalRepository(database, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
        revision = SQLiteDataLifecycle(database)
        with sqlite3.connect(database) as conn:
            current = conn.execute("SELECT server_revision FROM accounts WHERE id='old'").fetchone()[0]
        revision.delete_account("old", expected_server_revision=current, confirm_account_id="old")
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM places WHERE account_id='old'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
