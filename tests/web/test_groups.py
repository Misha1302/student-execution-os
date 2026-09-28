"""R8: collaborative academic groups over the canonical SOURCE/USER model.

LOCAL INTEGRATION: the real FastAPI app and SQLite database with several accounts.
Critical invariant: shared academic reality != personal execution state.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.persistence.sqlite import SCHEMA_VERSION
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient
from tests.rollback_chain import roll_back_newer_than

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)  # Monday 09:00 Moscow
GROUP = "group-bi-24-1"
SERIES = {"title": "Матанализ", "dtstart_local": "2026-09-22T10:00:00", "duration_minutes": 90,
          "recurrence_rule": "FREQ=WEEKLY;COUNT=10", "timezone_name": "Europe/Moscow",
          "location_text": "R205", "teacher": "Иванова", "category": "LESSON"}
EXAM = {"title": "Контрольная по матанализу", "starts_at": "2026-10-06T14:00:00+03:00",
        "ends_at": "2026-10-06T15:30:00+03:00", "category": "EXAM", "location_text": "R101"}


class GroupsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "groups.sqlite")
        self.client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
        self.users = {name: self.register(name) for name in ("owner", "bob", "carol", "dave")}

    def register(self, login):
        body = self.client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"}).json()
        return {"headers": {"Authorization": f"Bearer {body['token']}"}, "account": body["user"]["account_id"]}

    def h(self, name):
        return self.users[name]["headers"]

    def api(self, method, path, who, body=None, status=200):
        response = getattr(self.client, method)(path, headers=self.h(who), **({"json": body} if body is not None else {}))
        self.assertEqual(response.status_code, status, (method, path, response.text))
        return response.json()

    def revision(self, who="owner"):
        return self.api("get", f"/api/v1/groups/{GROUP}", who)["schedule_revision"]

    def publish(self, uid, kind, item, who="owner", status=200):
        return self.api("put", f"/api/v1/groups/{GROUP}/schedule/{uid}", who,
                        {"kind": kind, "item": item, "expected_revision": self.revision()}, status)

    def setup_group(self, members=("bob", "carol")):
        self.api("post", "/api/v1/groups", "owner", {"id": GROUP, "name": "БИ-24-1", "timezone_name": "Europe/Moscow"},
                 201)
        code = self.api("post", f"/api/v1/groups/{GROUP}/invitations", "owner", {"max_uses": 10}, 201)["code"]
        for member in members:
            self.api("post", "/api/v1/groups/join", member, {"code": code})
        return code

    def occurrences(self, who):
        body = self.api("get", "/api/v1/calendar", who)
        return [o for o in body["occurrences"] if o["title"] == "Матанализ"]

    def occ(self, who, rid):
        matches = [o for o in self.occurrences(who) if o["original_recurrence_id"] == rid]
        self.assertEqual(len(matches), 1, (who, rid, matches))  # stable identity: never duplicated
        return matches[0]

    def sync(self, who, *ops):
        return self.api("post", "/api/v1/sync", who, {"operations": list(ops)})["results"]

    # ---- membership ---------------------------------------------------------------

    def test_invitations_join_and_non_member_privacy(self):
        code = self.setup_group(members=("bob",))
        again = self.api("post", "/api/v1/groups/join", "bob", {"code": code})  # idempotent
        self.assertEqual(len(again["members"]), 2)
        self.assertEqual([g["role"] for g in self.api("get", "/api/v1/groups", "bob")], ["MEMBER"])
        # A non-member cannot learn the group exists.
        for method, path in (("get", f"/api/v1/groups/{GROUP}"), ("post", f"/api/v1/groups/{GROUP}/leave")):
            response = getattr(self.client, method)(path, headers=self.h("dave"))
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.api("get", "/api/v1/groups", "dave"), [])
        for bad in ("", "BOTAY-nope", code + "x"):
            self.api("post", "/api/v1/groups/join", "dave", {"code": bad}, 422)
        limited = self.api("post", f"/api/v1/groups/{GROUP}/invitations", "owner",
                           {"max_uses": 1, "expires_in_days": 1}, 201)
        self.api("post", "/api/v1/groups/join", "carol", {"code": limited["code"]})
        self.api("post", "/api/v1/groups/join", "dave", {"code": limited["code"]}, 422)  # used up
        revoked = self.api("post", f"/api/v1/groups/{GROUP}/invitations", "owner", {}, 201)
        self.api("delete", f"/api/v1/groups/{GROUP}/invitations/{revoked['invitation_id']}", "owner")
        self.api("post", "/api/v1/groups/join", "dave", {"code": revoked["code"]}, 422)
        members = self.api("get", f"/api/v1/groups/{GROUP}", "bob")["members"]
        self.assertEqual(sorted(m["login"] for m in members), ["bob", "carol", "owner"])
        self.assertEqual(set(members[0]), {"member_id", "login", "role", "joined_at", "me"})

    def test_roles_and_permissions(self):
        self.setup_group()
        bob, carol = self.users["bob"]["account"], self.users["carol"]["account"]
        # A member reads and proposes but cannot publish, invite, moderate or manage.
        self.publish("series-matan-001", "SERIES", SERIES, who="bob", status=403)
        self.api("post", f"/api/v1/groups/{GROUP}/invitations", "bob", {}, 403)
        self.api("delete", f"/api/v1/groups/{GROUP}/members/{carol}", "bob", status=403)
        self.api("post", f"/api/v1/groups/{GROUP}/members/{carol}/role", "bob", {"role": "OWNER"}, 403)
        # The owner makes bob starosta; the starosta publishes and invites.
        self.api("post", f"/api/v1/groups/{GROUP}/members/{bob}/role", "owner", {"role": "STAROSTA"})
        self.publish("series-matan-001", "SERIES", SERIES, who="bob")
        self.api("post", f"/api/v1/groups/{GROUP}/invitations", "bob", {}, 201)
        # A starosta may remove members, not the owner, and cannot change roles.
        owner = self.users["owner"]["account"]
        self.api("delete", f"/api/v1/groups/{GROUP}/members/{owner}", "bob", status=403)
        self.api("post", f"/api/v1/groups/{GROUP}/members/{carol}/role", "bob", {"role": "STAROSTA"}, 403)
        self.api("delete", f"/api/v1/groups/{GROUP}/members/{carol}", "bob")
        # The last owner can neither demote themselves nor leave while others remain.
        self.api("post", f"/api/v1/groups/{GROUP}/members/{owner}/role", "owner", {"role": "MEMBER"}, 422)
        self.api("post", f"/api/v1/groups/{GROUP}/leave", "owner", status=422)
        self.api("post", f"/api/v1/groups/{GROUP}/members/{bob}/role", "owner", {"role": "OWNER"})
        self.api("post", f"/api/v1/groups/{GROUP}/leave", "owner")
        self.assertEqual(self.api("get", f"/api/v1/groups/{GROUP}", "bob")["my_role"], "OWNER")

    def test_schedule_concurrency_and_validation(self):
        self.setup_group()
        stale = self.revision()
        self.publish("series-matan-001", "SERIES", SERIES)
        conflict = self.client.put(f"/api/v1/groups/{GROUP}/schedule/series-exam-0001", headers=self.h("owner"),
                                   json={"kind": "EVENT", "item": EXAM, "expected_revision": stale})
        self.assertEqual(conflict.status_code, 409)
        for kind, item in (("SERIES", {**SERIES, "recurrence_rule": "FREQ=HOURLY"}),
                           ("SERIES", {**SERIES, "duration_minutes": 0}),
                           ("EVENT", {**EXAM, "ends_at": EXAM["starts_at"]}),
                           ("EVENT", {**EXAM, "category": "PERSONAL"}),
                           ("TASK", {"title": "x"})):
            self.publish("series-bad-00001", kind, item, status=422)
        self.publish("series-matan-001", "EVENT", EXAM, status=422)  # kind cannot change

    def test_concurrent_publishers_cannot_both_win(self):
        self.setup_group()
        bob = self.users["bob"]["account"]
        self.api("post", f"/api/v1/groups/{GROUP}/members/{bob}/role", "owner", {"role": "STAROSTA"})
        revision = self.revision()
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from student_execution_os.groups import GroupService
        from student_execution_os.persistence import SQLiteCanonicalRepository
        from student_execution_os.domain.clock import FrozenClock
        from student_execution_os.domain.errors import VersionConflict
        barrier = Barrier(2)

        def publish(who, room):
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                service = GroupService(repo, account_id=self.users[who]["account"])
                barrier.wait()
                try:
                    service.publish(GROUP, "series-matan-001", {"kind": "SERIES", "expected_revision": revision,
                                                                "item": {**SERIES, "location_text": room}})
                    return "published"
                except VersionConflict:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(publish, ("owner", "bob"), ("R1", "R2")))
        self.assertEqual(outcomes, ["conflict", "published"])
        self.assertEqual(self.revision(), revision + 1)

    # ---- the critical invariant ---------------------------------------------------------

    def test_personal_overlay_survives_group_changes_and_never_leaks(self):
        self.setup_group()
        self.publish("series-matan-001", "SERIES", SERIES)
        moved, cancelled = "2026-09-29T10:00:00", "2026-10-06T10:00:00"
        bob_template = self.occ("bob", moved)["template_id"]
        carol_template = self.occ("carol", moved)["template_id"]
        self.assertNotEqual(bob_template, carol_template)  # each account has its own canonical copy
        results = self.sync(
            "bob",
            {"op_id": "bob-move-0001", "type": "series.occurrence.move", "entity_id": bob_template,
             "payload": {"template_id": bob_template, "original_recurrence_id": moved,
                         "starts_local": "2026-09-29T17:00:00"}},
            {"op_id": "bob-note-0001", "type": "series.occurrence.update", "entity_id": bob_template,
             "payload": {"template_id": bob_template, "original_recurrence_id": moved,
                         "note": "сдать долг лично преподавателю"}},
            {"op_id": "bob-skip-0001", "type": "series.occurrence.cancel", "entity_id": bob_template,
             "payload": {"template_id": bob_template, "original_recurrence_id": cancelled}},
            {"op_id": "bob-task-0001", "type": "task.create", "entity_id": "bob-private-task",
             "payload": {"title": "Подготовиться к пересдаче"}},
            {"op_id": "bob-rem-00001", "type": "reminder.create", "entity_id": "bob-private-rem",
             "payload": {"title": "Взять зачётку", "remind_at": "2026-09-29T13:00:00+00:00"}},
        )
        self.assertTrue(all(r["status"] == "APPLIED" for r in results), results)

        # The starosta changes shared reality: new room for all classes, one class moved
        # by the university, and an exam is added.
        changed = {**SERIES, "location_text": "R310",
                   "changes": [{"recurrence_local": "2026-10-13T10:00:00", "starts_local": "2026-10-13T12:00:00"}]}
        self.publish("series-matan-001", "SERIES", changed)
        self.publish("event-exam-00001", "EVENT", EXAM)

        # Bob: his personal move, note and skip survive; unaffected classes follow the group.
        mine = self.occ("bob", moved)
        self.assertEqual(mine["starts_at"][:16], "2026-09-29T14:00")  # 17:00 Moscow, his move
        self.assertEqual(mine["location_text"], "R310")
        self.assertTrue(self.occ("bob", cancelled)["cancelled"])
        self.assertEqual(self.occ("bob", "2026-10-13T10:00:00")["starts_at"][:16], "2026-10-13T09:00")
        self.assertEqual(self.occ("bob", "2026-10-20T10:00:00")["location_text"], "R310")
        # Carol sees only group reality: none of Bob's personal changes.
        hers = self.occ("carol", moved)
        self.assertEqual((hers["starts_at"][:16], hers["cancelled"]), ("2026-09-29T07:00", False))
        self.assertFalse(self.occ("carol", cancelled)["cancelled"])
        carol_view = json.dumps([self.api("get", p, "carol") for p in
                                 ("/api/v1/calendar", "/api/v1/tasks", "/api/v1/reminders", "/api/v1/today")],
                                ensure_ascii=False)
        for private in ("сдать долг", "Подготовиться к пересдаче", "Взять зачётку", "17:00"):
            self.assertNotIn(private, carol_view)
        # The group (even its owner) never sees personal state.
        group_view = json.dumps(self.api("get", f"/api/v1/groups/{GROUP}", "owner"), ensure_ascii=False)
        for private in ("сдать долг", "пересдаче", "зачётку", "2026-09-29T17:00"):
            self.assertNotIn(private, group_view)
        # Both see the exam as a canonical event.
        for who in ("bob", "carol"):
            self.assertIn("Контрольная по матанализу", [e["title"] for e in self.api("get", "/api/v1/events", who)])

    def test_proposals_workflow(self):
        self.setup_group()
        self.publish("series-matan-001", "SERIES", SERIES)
        proposal = {"id": "prop-exam-00001", "action": "UPSERT", "uid": "event-exam-00001", "kind": "EVENT",
                    "item": EXAM, "note": "Препод сказал на паре"}
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "bob", proposal, 201)
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "bob", proposal, 201)  # idempotent retry
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "carol", proposal, 422)  # id taken
        # Pending: nothing published; the proposer and staff see it, other members do not.
        self.assertNotIn("Контрольная по матанализу", [e["title"] for e in self.api("get", "/api/v1/events", "bob")])
        self.assertEqual(len(self.api("get", f"/api/v1/groups/{GROUP}", "owner")["proposals"]), 1)
        self.assertEqual(self.api("get", f"/api/v1/groups/{GROUP}", "carol")["proposals"], [])
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-exam-00001/decide", "carol",
                 {"decision": "APPROVE"}, 403)
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-exam-00001/decide", "owner", {"decision": "APPROVE"})
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-exam-00001/decide", "owner", {"decision": "APPROVE"})
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-exam-00001/decide", "owner",
                 {"decision": "REJECT"}, 409)
        for who in ("bob", "carol", "owner"):
            self.assertIn("Контрольная по матанализу", [e["title"] for e in self.api("get", "/api/v1/events", who)])
        # A rejected removal changes nothing; a withdrawn proposal cannot be decided.
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "carol",
                 {"id": "prop-remove-0001", "action": "REMOVE", "uid": "series-matan-001", "note": "отменили"}, 201)
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-remove-0001/decide", "owner",
                 {"decision": "REJECT", "reason": "не подтверждено"})
        self.assertTrue(self.occurrences("carol"))
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "carol",
                 {"id": "prop-remove-0002", "action": "REMOVE", "uid": "series-matan-001"}, 201)
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-remove-0002/withdraw", "carol")
        self.api("post", f"/api/v1/groups/{GROUP}/proposals/prop-remove-0002/decide", "owner",
                 {"decision": "APPROVE"}, 409)
        self.api("post", f"/api/v1/groups/{GROUP}/proposals", "bob",
                 {"id": "prop-bad-00001", "action": "UPSERT", "uid": "event-bad-00001", "kind": "EVENT",
                  "item": {**EXAM, "ends_at": "2026-10-20T00:00:00+03:00"}}, 422)

    def test_leaving_retracts_shared_classes_keeps_personal_state_and_rejoin_restores(self):
        code = self.setup_group()
        self.publish("series-matan-001", "SERIES", SERIES)
        rid = "2026-09-29T10:00:00"
        template = self.occ("bob", rid)["template_id"]
        self.sync("bob",
                  {"op_id": "bob-move-0002", "type": "series.occurrence.move", "entity_id": template,
                   "payload": {"template_id": template, "original_recurrence_id": rid,
                               "starts_local": "2026-09-29T18:00:00"}},
                  {"op_id": "bob-task-0002", "type": "task.create", "entity_id": "bob-own-task-01",
                   "payload": {"title": "Личное дело"}})
        self.api("post", f"/api/v1/groups/{GROUP}/leave", "bob")
        self.assertEqual(self.occurrences("bob"), [])
        self.assertEqual([t["title"] for t in self.api("get", "/api/v1/tasks", "bob")], ["Личное дело"])
        self.assertTrue(self.occurrences("carol"))  # others unaffected
        self.api("post", "/api/v1/groups/join", "bob", {"code": code})
        again = self.occ("bob", rid)
        self.assertEqual((again["template_id"], again["starts_at"][:16]), (template, "2026-09-29T15:00"))
        # Removal also retracts, and a removed member cannot rejoin with a code.
        self.api("delete", f"/api/v1/groups/{GROUP}/members/{self.users['carol']['account']}", "owner")
        self.assertEqual(self.occurrences("carol"), [])
        self.api("post", "/api/v1/groups/join", "carol", {"code": code}, 403)
        self.assertEqual(self.api("get", f"/api/v1/groups/{GROUP}", "carol", status=404)["error"]["code"],
                         "NOT_FOUND")

    def test_unpublish_removes_the_class_everywhere_and_account_deletion(self):
        self.setup_group()
        bob, owner = self.users["bob"]["account"], self.users["owner"]["account"]
        self.api("post", f"/api/v1/groups/{GROUP}/members/{bob}/role", "owner", {"role": "STAROSTA"})
        self.publish("series-matan-001", "SERIES", SERIES)
        self.api("post", f"/api/v1/groups/{GROUP}/schedule/series-matan-001/remove", "owner",
                 {"expected_revision": self.revision()})
        for who in ("owner", "bob", "carol"):
            self.assertEqual(self.occurrences(who), [])
        from student_execution_os.reliability import SQLiteDataLifecycle
        with sqlite3.connect(self.db) as conn:
            revision = conn.execute("SELECT server_revision FROM accounts WHERE id=?", (owner,)).fetchone()[0]
        SQLiteDataLifecycle(self.db, now=lambda: NOW).delete_account(owner, expected_server_revision=revision,
                                                                      confirm_account_id=owner)
        # The group outlives its owner: the starosta inherits it.
        detail = self.api("get", f"/api/v1/groups/{GROUP}", "bob")
        self.assertEqual(detail["my_role"], "OWNER")
        self.assertEqual(sorted(m["login"] for m in detail["members"]), ["bob", "carol"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v26_to_v27_upgrade_and_fail_closed_rollback(self):
        old = str(Path(self.tmp.name) / "v26.sqlite")
        migrations = Path("src/student_execution_os/persistence/migrations")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version in range(1, 27):
            conn.executescript(next(migrations.glob(f"{version:03d}_*.sql")).read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, NOW.isoformat()))
        conn.commit()
        conn.close()
        from student_execution_os.persistence import SQLiteCanonicalRepository
        with SQLiteCanonicalRepository(old) as repo:
            repo.initialize()
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn = sqlite3.connect(old)
        roll_back_newer_than(conn, 26)
        self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 26)
        conn.close()
        self.setup_group(members=())
        conn = sqlite3.connect(self.db)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "deleted first"):
            roll_back_newer_than(conn, 26)
        conn.close()


if __name__ == "__main__":
    unittest.main()
