"""Collaborative groups (schema v18, ADR 0021) through the real operation pipeline.

Every mutation goes through ``SyncService.apply`` exactly as the HTTP routes and the
offline queue send it: op ids, one transaction, replay, conflicts."""
from __future__ import annotations

import itertools
import json
import unittest
from datetime import datetime, timedelta, timezone

from student_execution_os.connectors.repository import SQLiteConnectorRepository
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.groups.projection import PersonalProjection
from student_execution_os.groups.reconcile import reconcile_external_bindings
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import PlanningService
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.reminders.engine import ReminderEngine, task_facts
from student_execution_os.reminders.policy import ReminderPrefs, decide
from student_execution_os.sync.commands import SyncService

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
T = lambda days=0, hours=0, minutes=0: (NOW + timedelta(days=days, hours=hours, minutes=minutes)).isoformat()  # noqa: E731


class World:
    """A database with four people: the group's owner (starosta), a scheduler, a member and an outsider."""

    def __init__(self, database: str = ":memory:") -> None:
        self.clock = FrozenClock(NOW)
        self.repo = SQLiteCanonicalRepository(database, clock=self.clock)
        self.repo.initialize()
        for account in ("owner", "sched", "member", "other", "out"):
            self.repo.create_account(account)
        self.ids = itertools.count()

    def op(self, account: str, op_type: str, entity_id: str = "", payload: dict | None = None, *, op_id: str | None = None,
           channel: str = "direct") -> dict:
        op_id = op_id or f"op-{next(self.ids):08d}"
        return SyncService(self.repo, account_id=account, principal_id=account, now=self.clock.now(),
                           channel=channel).apply({"op_id": op_id, "type": op_type, "entity_id": entity_id,
                                                   "payload": payload or {}})

    def ok(self, *args, **kwargs) -> dict:
        result = self.op(*args, **kwargs)
        assert result["status"] in ("APPLIED", "NOOP"), result
        return result["entity"]

    def group(self, group_id: str = "group-pi261", *, approval: bool = False, members_publish: bool = False) -> str:
        self.ok("owner", "group.create", group_id, {"name": "ПИ-261", "settings": {"default_timezone": "Europe/Moscow",
                                                                                   "allow_members_to_publish": members_publish},
                                                     "join_policy": {"approval_required": approval}})
        code = self.ok("owner", "invite.create", group_id, {"kind": "CODE"})["code"]
        for account in ("sched", "member", "other"):
            self.ok(account, "group.join", "", {"code": code})
        if not approval:
            version = self.repo.connection.execute(
                "SELECT version FROM group_memberships WHERE group_id=? AND account_id='sched'", (group_id,)).fetchone()[0]
            self.ok("owner", "membership.update", group_id, {"account_id": "sched", "role": "SCHEDULER",
                                                             "expected_version": version})
        return group_id

    def control_work(self, event_id: str = "sev-kr2-0001", group_id: str = "group-pi261", **extra) -> dict:
        payload = {"group_id": group_id, "title": "Контрольная работа №2", "event_kind": "CONTROL_WORK",
                   "starts_at": T(days=14, hours=0, minutes=10), "ends_at": T(days=14, hours=1, minutes=30),
                   "location": "R201", **extra}
        return self.ok("sched", "shared_event.create", event_id, payload)

    def item(self, account: str, entity_id: str) -> dict:
        return next(i for i in PersonalProjection(self.repo, account, self.clock.now()).items() if i["id"] == entity_id)

    def count(self, table: str, where: str = "1=1", args: tuple = ()) -> int:
        return int(self.repo.connection.execute(f"SELECT count(*) FROM {table} WHERE {where}", args).fetchone()[0])


class PermissionsTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def test_member_cannot_edit_published_event_scheduler_can(self):
        refused = self.w.op("member", "shared_event.update", self.event["id"], {"expected_version": 1, "title": "hack"})
        self.assertEqual((refused["status"], refused["code"]), ("REJECTED", "FORBIDDEN"))
        refused = self.w.op("member", "shared_event.cancel", self.event["id"], {"expected_version": 1})
        self.assertEqual(refused["code"], "FORBIDDEN")
        edited = self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "title": "КР №2"})
        self.assertEqual((edited["title"], edited["version"]), ("КР №2", 2))

    def test_member_publication_needs_a_proposal_unless_the_group_allows_it(self):
        refused = self.w.op("member", "shared_event.create", "sev-member-0001",
                            {"group_id": "group-pi261", "title": "Квиз", "event_kind": "QUIZ",
                             "starts_at": T(days=2), "ends_at": T(days=2, hours=1)})
        self.assertEqual(refused["code"], "PROPOSAL_REQUIRED")
        self.assertIsNone(self.w.repo.connection.execute("SELECT 1 FROM shared_events WHERE id='sev-member-0001'").fetchone())
        trusted = World()
        trusted.group(members_publish=True)
        created = trusted.ok("member", "shared_event.create", "sev-member-0001",
                             {"group_id": "group-pi261", "title": "Квиз", "event_kind": "QUIZ",
                              "starts_at": T(days=2), "ends_at": T(days=2, hours=1)})
        self.assertEqual(created["status"], "PUBLISHED")

    def test_outsider_cannot_read_or_touch_group_data(self):
        from student_execution_os.groups.service import GroupReadService
        from student_execution_os.domain.errors import EntityNotFound
        service = GroupReadService(self.w.repo, "out", NOW)
        for call in (lambda: service.group("group-pi261"), lambda: service.members("group-pi261"),
                     lambda: service.entity(__import__("student_execution_os.groups.model", fromlist=["SharedKind"]).SharedKind.SHARED_EVENT, self.event["id"]),
                     lambda: service.proposals("group-pi261")):
            with self.assertRaises(EntityNotFound):
                call()
        for op, payload in (("shared_event_state.update", {"muted": True}), ("shared_event.cancel", {"expected_version": 1})):
            result = self.w.op("out", op, self.event["id"], payload)
            self.assertEqual(result["code"], "NOT_FOUND")
        self.assertEqual(PersonalProjection(self.w.repo, "out", NOW).items(), [])

    def test_admin_capabilities_are_explicit_not_ordinal(self):
        # A scheduler publishes and moderates but cannot manage members, invites or roles.
        refused = self.w.op("sched", "invite.create", "group-pi261", {"kind": "LINK"})
        self.assertEqual(refused["code"], "FORBIDDEN")
        member_version = self.w.repo.connection.execute(
            "SELECT version FROM group_memberships WHERE account_id='member'").fetchone()[0]
        refused = self.w.op("sched", "membership.update", "group-pi261",
                            {"account_id": "member", "status": "REMOVED", "expected_version": member_version})
        self.assertEqual(refused["code"], "FORBIDDEN")
        # The client cannot claim a role.
        refused = self.w.op("member", "membership.update", "group-pi261",
                            {"account_id": "other", "role": "ADMIN", "expected_version": 1})
        self.assertEqual(refused["code"], "FORBIDDEN")

    def test_owner_and_admin_cannot_read_a_members_personal_overlay(self):
        self.w.ok("member", "shared_event_state.update", self.event["id"],
                  {"criticality_override": "NORMAL", "attendance_override": "SKIP", "remind_before_minutes": 30})
        self.w.ok("member", "task.create", "task-member-prep", {"title": "Готовлюсь тайно", "estimated_total_effort_minutes": 60,
                                                                "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}})
        owner_view = self.w.item("owner", self.event["id"])
        self.assertEqual(owner_view["criticality"]["effective"], "CRITICAL")
        self.assertIsNone(owner_view["personal"]["preparation_task"])
        self.assertIsNone(owner_view["personal"]["remind_before_minutes"])
        from student_execution_os.groups.service import GroupReadService
        service = GroupReadService(self.w.repo, "owner", NOW)
        dump = json.dumps([service.group("group-pi261"), service.members("group-pi261"),
                           service.entities(__import__("student_execution_os.groups.model", fromlist=["SharedKind"]).SharedKind.SHARED_EVENT, "group-pi261"),
                           service.audit("group-pi261")], ensure_ascii=False)
        for secret in ("task-member-prep", "Готовлюсь тайно", "SKIP", "attendance_override", "remind_before"):
            self.assertNotIn(secret, dump)
        # The personal API of another account's overlay is simply the caller's own.
        self.assertIsNone(self.w.item("other", self.event["id"])["personal"]["remind_before_minutes"])


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def test_reschedule_updates_one_entity_and_keeps_overrides(self):
        self.w.ok("member", "shared_event_state.update", self.event["id"], {"criticality_override": "IMPORTANT",
                                                                            "attendance_override": "PREFERRED"})
        moved = self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "starts_at": T(days=15, hours=3)})
        self.assertEqual((moved["id"], moved["version"]), (self.event["id"], 2))
        # Duration kept.
        self.assertEqual(datetime.fromisoformat(moved["ends_at"]) - datetime.fromisoformat(moved["starts_at"]), timedelta(minutes=80))
        self.assertEqual(self.w.count("shared_events"), 1)
        item = self.w.item("member", self.event["id"])
        self.assertEqual(item["criticality"]["effective"], "IMPORTANT")
        self.assertEqual(item["attendance"]["effective"], "PREFERRED")
        self.assertEqual(item["change"]["changes"]["starts_at"][1], moved["starts_at"])

    def test_stale_version_conflicts_with_current_version(self):
        self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "location": "R301"})
        stale = self.w.op("owner", "shared_event.update", self.event["id"], {"expected_version": 1, "location": "R999"})
        self.assertEqual((stale["status"], stale["code"], stale["current_version"]), ("CONFLICT", "VERSION_CONFLICT", 2))
        self.assertEqual(self.w.repo.connection.execute("SELECT location FROM shared_events").fetchone()[0], "R301")

    def test_version_is_required_no_silent_last_writer_wins(self):
        refused = self.w.op("sched", "shared_event.update", self.event["id"], {"title": "без версии"})
        self.assertEqual(refused["status"], "REJECTED")
        self.assertIn("expected_version", refused["message"])

    def test_replay_of_the_same_mutation_creates_nothing_twice(self):
        payload = {"group_id": "group-pi261", "title": "Квиз", "event_kind": "QUIZ", "starts_at": T(days=3), "ends_at": T(days=3, hours=1)}
        first = self.w.op("sched", "shared_event.create", "sev-quiz-0001", payload, op_id="mutation-quiz-1")
        replay = self.w.op("sched", "shared_event.create", "sev-quiz-0001", payload, op_id="mutation-quiz-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["entity"], replay["entity"])
        # A new mutation id with the same client entity id is still one entity.
        again = self.w.op("sched", "shared_event.create", "sev-quiz-0001", payload)
        self.assertEqual(again["code"], "ALREADY_EXISTS")
        self.assertEqual(self.w.count("shared_events", "id='sev-quiz-0001'"), 1)
        # Same op id with a different payload is refused, never applied.
        reused = self.w.op("sched", "shared_event.create", "sev-quiz-0001", {**payload, "title": "x"}, op_id="mutation-quiz-1")
        self.assertEqual(reused["code"], "OP_ID_REUSED")

    def test_audit_records_who_changed_the_critical_event_and_when(self):
        self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "starts_at": T(days=16)},
                  op_id="mutation-move-1")
        row = self.w.repo.connection.execute(
            "SELECT * FROM group_audit WHERE entity_id=? AND action='UPDATE'", (self.event["id"],)).fetchone()
        self.assertEqual((row["actor_account_id"], row["previous_version"], row["new_version"], row["mutation_id"]),
                         ("sched", 1, 2, "mutation-move-1"))
        self.assertIn("starts_at", json.loads(row["changes_json"]))


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def test_cancelled_event_cannot_be_republished_by_patch(self):
        self.w.ok("sched", "shared_event.cancel", self.event["id"], {"expected_version": 1})
        patched = self.w.op("sched", "shared_event.update", self.event["id"], {"expected_version": 2, "title": "ожила"})
        self.assertEqual((patched["status"], patched["code"]), ("CONFLICT", "ENTITY_CANCELLED"))
        status = self.w.op("sched", "shared_event.update", self.event["id"], {"expected_version": 2, "status": "PUBLISHED"})
        self.assertEqual(status["status"], "REJECTED")
        self.assertEqual(self.w.repo.connection.execute("SELECT status FROM shared_events").fetchone()[0], "CANCELLED")
        again = self.w.op("sched", "shared_event.cancel", self.event["id"], {"expected_version": 2})
        self.assertEqual(again["code"], "ALREADY_CANCELLED")

    def test_obligation_and_announcement_terminal_states(self):
        self.w.ok("sched", "shared_obligation.create", "sob-lab-0001", {"group_id": "group-pi261", "title": "Лаба 3",
                                                                         "deadline": T(days=5)})
        self.w.ok("sched", "shared_obligation.cancel", "sob-lab-0001", {"expected_version": 1})
        self.assertEqual(self.w.op("sched", "shared_obligation.update", "sob-lab-0001",
                                   {"expected_version": 2, "title": "x"})["code"], "ENTITY_CANCELLED")
        self.w.ok("sched", "announcement.create", "san-news-0001", {"group_id": "group-pi261", "title": "Пара в Zoom",
                                                                     "body": "Ссылка у старосты"})
        self.w.ok("sched", "announcement.retract", "san-news-0001", {"expected_version": 1})
        self.assertEqual(self.w.op("sched", "announcement.update", "san-news-0001",
                                   {"expected_version": 2, "body": "x"})["code"], "ENTITY_RETRACTED")

    def test_group_status_machine(self):
        self.w.ok("owner", "group.archive", "group-pi261", {"expected_version": 1})
        refused = self.w.op("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "title": "x"})
        self.assertEqual(refused["code"], "FORBIDDEN")  # archived groups are read-only
        self.w.ok("owner", "group.unarchive", "group-pi261", {"expected_version": 2})
        self.w.ok("owner", "group.delete", "group-pi261", {"expected_version": 3})
        after = self.w.op("owner", "group.unarchive", "group-pi261", {"expected_version": 4})
        self.assertEqual(after["code"], "NOT_FOUND")  # DELETED is terminal; the row stays as a tombstone
        self.assertEqual(self.w.count("groups", "status='DELETED'"), 1)

    def test_blocked_member_cannot_rejoin_with_an_invite(self):
        version = self.w.repo.connection.execute("SELECT version FROM group_memberships WHERE account_id='other'").fetchone()[0]
        self.w.ok("owner", "membership.update", "group-pi261", {"account_id": "other", "status": "BLOCKED",
                                                               "expected_version": version})
        code = self.w.ok("owner", "invite.create", "group-pi261", {"kind": "CODE"})["code"]
        refused = self.w.op("other", "group.join", "", {"code": code})
        self.assertEqual(refused["code"], "JOIN_NOT_ALLOWED")
        self.assertEqual(self.w.op("other", "shared_event_state.update", self.event["id"], {"muted": True})["code"], "NOT_FOUND")
        # Only an explicit admin action brings them back.
        self.w.ok("owner", "membership.update", "group-pi261", {"account_id": "other", "status": "ACTIVE",
                                                               "expected_version": version + 1})
        self.assertEqual(self.w.item("other", self.event["id"])["id"], self.event["id"])

    def test_left_member_can_rejoin_removed_only_when_policy_allows(self):
        self.w.ok("member", "group.leave", "group-pi261", {})
        code = self.w.ok("owner", "invite.create", "group-pi261", {"kind": "CODE"})["code"]
        self.w.ok("member", "group.join", "", {"code": code})
        version = self.w.repo.connection.execute("SELECT version FROM group_memberships WHERE account_id='other'").fetchone()[0]
        self.w.ok("owner", "membership.update", "group-pi261", {"account_id": "other", "status": "REMOVED",
                                                               "expected_version": version})
        self.assertEqual(self.w.op("other", "group.join", "", {"code": code})["code"], "JOIN_NOT_ALLOWED")

    def test_revoked_or_unknown_invites_do_not_work(self):
        invite = self.w.ok("owner", "invite.create", "group-pi261", {"kind": "LINK"})
        self.assertIn("token", invite)  # shown once …
        stored = self.w.repo.connection.execute("SELECT result_json FROM client_operations WHERE op_type='invite.create' "
                                                "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
        self.assertNotIn(invite["token"], stored)  # … and never stored, not even in the operation log
        self.assertNotIn(invite["token"], json.dumps([dict(r) for r in self.w.repo.connection.execute("SELECT * FROM group_invites")]))
        self.w.ok("owner", "invite.revoke", invite["id"], {"expected_version": 1})
        self.w.repo.create_account("newbie")
        self.assertEqual(self.w.op("newbie", "group.join", "", {"token": invite["token"]})["code"], "INVITE_UNAVAILABLE")
        self.assertEqual(self.w.op("newbie", "group.join", "", {"token": "guess"})["code"], "INVITE_UNAVAILABLE")

    def test_join_attempts_are_rate_limited(self):
        from student_execution_os.groups.errors import GroupRateLimited
        self.w.repo.create_account("guesser")
        for n in range(20):
            self.w.op("guesser", "group.join", "", {"code": f"ZZZZ-{n:04d}"})
        with self.assertRaises(GroupRateLimited):
            self.w.op("guesser", "group.join", "", {"code": "ZZZZ-9999"})

    def test_approval_required_puts_joiners_in_pending(self):
        w = World()
        w.group(approval=True)
        self.assertEqual(w.count("group_memberships", "status='PENDING'"), 3)
        self.assertEqual(w.op("member", "shared_event_state.update", "x" * 8, {})["code"], "NOT_FOUND")
        w.ok("owner", "membership.update", "group-pi261", {"account_id": "member", "status": "ACTIVE", "expected_version": 1})
        self.assertEqual(w.count("reminder_messages", "account_id='member'"), 1)  # "you were admitted"


class ProposalTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.payload = {"kind": "SHARED_EVENT", "title": "Квиз по АиСД", "event_kind": "QUIZ",
                        "starts_at": T(days=17), "ends_at": T(days=17, minutes=30)}

    def propose(self, proposal_id="prop-quiz-0001"):
        return self.w.ok("member", "proposal.create", proposal_id, {"group_id": "group-pi261", "payload": self.payload})

    def test_member_proposal_publishes_nothing_until_approved_then_exactly_one(self):
        proposal = self.propose()
        self.assertEqual(proposal["status"], "PENDING")
        self.assertEqual(self.w.count("shared_events"), 0)
        self.assertEqual([i for i in PersonalProjection(self.w.repo, "other", NOW).items()], [])
        approved = self.w.ok("sched", "proposal.approve", proposal["id"], {"expected_version": 1}, op_id="approve-once-1")
        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(self.w.count("shared_events"), 1)
        event = self.w.repo.connection.execute("SELECT * FROM shared_events").fetchone()
        self.assertEqual((event["id"], event["proposal_id"], event["status"]), (approved["approved_entity_id"], proposal["id"], "PUBLISHED"))
        # Duplicate approval: the same op id (retry), another op id, another moderator — one entity.
        replay = self.w.op("sched", "proposal.approve", proposal["id"], {"expected_version": 1}, op_id="approve-once-1")
        second = self.w.op("owner", "proposal.approve", proposal["id"], {"expected_version": 1})
        self.assertTrue(replay["replayed"])
        self.assertEqual((second["status"], second["code"]), ("NOOP", "ALREADY_APPROVED"))
        self.assertEqual(second["entity"]["approved_entity_id"], approved["approved_entity_id"])
        self.assertEqual(self.w.count("shared_events"), 1)
        # Every member now sees the one agreed event.
        self.assertEqual(self.w.item("other", approved["approved_entity_id"])["title"], "Квиз по АиСД")

    def test_edit_and_approve_keeps_original_and_approved_payload_apart(self):
        proposal = self.propose()
        approved = self.w.ok("sched", "proposal.approve", proposal["id"], {"expected_version": 1,
                                                                           "edits": {"title": "Квиз №1 по АиСД"}})
        self.assertEqual(approved["payload"]["title"], "Квиз по АиСД")
        self.assertEqual(approved["approved_payload"]["title"], "Квиз №1 по АиСД")
        audit = self.w.repo.connection.execute("SELECT changes_json FROM group_audit WHERE action='APPROVE'").fetchone()[0]
        self.assertEqual(json.loads(audit)["edited"]["title"], ["Квиз по АиСД", "Квиз №1 по АиСД"])

    def test_invalid_payload_is_refused_like_a_publication(self):
        bad = self.w.op("member", "proposal.create", "prop-bad-00001",
                        {"group_id": "group-pi261", "payload": {**self.payload, "ends_at": T(days=16)}})
        self.assertEqual(bad["status"], "REJECTED")
        naive = self.w.op("member", "proposal.create", "prop-bad-00002",
                          {"group_id": "group-pi261", "payload": {**self.payload, "starts_at": "2026-10-18T10:00"}})
        self.assertIn("UTC offset", naive["message"])
        blob = self.w.op("member", "proposal.create", "prop-bad-00003",
                         {"group_id": "group-pi261", "payload": {**self.payload, "anything": 1}})
        self.assertIn("unknown fields", blob["message"])

    def test_reject_withdraw_and_terminal_proposals(self):
        proposal = self.propose()
        self.w.ok("sched", "proposal.reject", proposal["id"], {"expected_version": 1, "comment": "уже есть"})
        again = self.w.op("sched", "proposal.approve", proposal["id"], {"expected_version": 2})
        self.assertEqual((again["status"], again["code"]), ("CONFLICT", "PROPOSAL_CLOSED"))
        self.assertEqual(self.w.count("shared_events"), 0)
        other = self.propose("prop-quiz-0002")
        self.assertEqual(self.w.op("other", "proposal.withdraw", other["id"], {"expected_version": 1})["code"], "FORBIDDEN")
        self.w.ok("member", "proposal.withdraw", other["id"], {"expected_version": 1})
        self.assertEqual(self.w.op("sched", "proposal.approve", other["id"], {"expected_version": 2})["code"], "PROPOSAL_CLOSED")

    def test_duplicate_pending_suggestion_and_rate_limit(self):
        first = self.propose()
        duplicate = self.w.op("other", "proposal.create", "prop-quiz-0009", {"group_id": "group-pi261", "payload": self.payload})
        self.assertEqual((duplicate["code"], duplicate["entity"]["id"]), ("DUPLICATE_PROPOSAL", first["id"]))
        from student_execution_os.groups.errors import GroupRateLimited
        with self.assertRaises(GroupRateLimited):
            for n in range(12):
                self.w.op("member", "proposal.create", f"prop-spam-{n:04d}",
                          {"group_id": "group-pi261", "payload": {**self.payload, "title": f"spam {n}"}})


class AssessmentAndReminderTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def test_assessment_is_a_shared_event_with_critical_defaults(self):
        self.assertEqual((self.event["kind"], self.event["is_assessment"]), ("SHARED_EVENT", True))
        self.assertEqual((self.event["group_criticality"], self.event["attendance_default"]), ("CRITICAL", "REQUIRED"))
        tables = {r[0] for r in self.w.repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any("assessment" in t for t in tables))

    def test_preparation_is_a_private_task_the_planner_schedules(self):
        task = self.w.ok("member", "task.create", "task-prep-kr2", {"title": "Подготовиться к КР", "estimated_total_effort_minutes": 240,
                                                                   "splittable": True, "min_chunk_minutes": 30, "max_chunk_minutes": 120,
                                                                   "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}})
        self.assertEqual(task["actual_cutoff"]["at"], self.event["starts_at"])
        self.assertEqual(task["importance"], "CRITICAL")
        from student_execution_os.web.queries import UiService  # noqa: F401  (planner path used by the product)
        from student_execution_os.planning import build_planning_snapshot, SQLitePlanningStateSource
        from student_execution_os.planning.model import PlanningPolicy
        from student_execution_os.reminders.engine import derived_availability
        from student_execution_os.planning.outlook import SQLitePlanningProfileRepository
        profile = SQLitePlanningProfileRepository(self.w.repo).get("member")
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(self.w.repo), account_id="member", analysis_horizon_start=NOW,
            analysis_horizon_end=NOW + timedelta(days=15), plan_output_horizon_end=NOW + timedelta(days=15),
            policy=PlanningPolicy(), derived_constraints=derived_availability(self.w.repo, profile, "member", NOW),
            assume_attendance=True)
        plan = PlanningService().build(snapshot, now=NOW).plan
        work = [b for b in plan.blocks if b.obligation_id == "task-prep-kr2"]
        self.assertEqual(sum(b.duration_minutes for b in work), 240)
        self.assertTrue(all(b.ends_at <= datetime.fromisoformat(self.event["starts_at"]) for b in work))
        # The required shared event itself is busy time for the member (derived, not stored).
        self.assertTrue(any(c.id == f"shared:{self.event['id']}" for c in snapshot.constraints))
        self.assertEqual(self.w.count("user_time_constraints"), 0)
        # The group sees nothing of it.
        self.assertNotIn("task-prep-kr2", json.dumps(self.w.item("owner", self.event["id"])))

    def test_reschedule_moves_preparation_deadline_and_reminders(self):
        self.w.ok("member", "task.create", "task-prep-kr2", {"title": "Подготовка", "estimated_total_effort_minutes": 120,
                                                             "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}})
        self.w.ok("other", "task.create", "task-prep-own", {"title": "Своя подготовка", "estimated_total_effort_minutes": 60,
                                                            "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]},
                                                            "actual_cutoff": {"state": "KNOWN", "at": T(days=12)}})
        self.w.ok("member", "shared_event_state.update", self.event["id"], {"remind_before_minutes": 60, "alarm_before_minutes": 90})
        new_start = T(days=15, hours=3)
        self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "starts_at": new_start})
        task = self.w.repo.get_task("member", "task-prep-kr2")
        self.assertEqual(task.actual_cutoff.at.isoformat(), new_start)
        # A deadline the member chose themselves is not overwritten; the app offers to update it.
        self.assertEqual(self.w.repo.get_task("other", "task-prep-own").actual_cutoff.at.isoformat(), T(days=12))
        self.assertTrue(self.w.item("other", self.event["id"])["personal"]["preparation_deadline_stale"])
        remind = self.w.repo.connection.execute(
            "SELECT remind_at FROM reminder_states WHERE account_id='member' AND task_id=?", (f"shared-event:{self.event['id']}",)).fetchone()[0]
        self.assertEqual(datetime.fromisoformat(remind), datetime.fromisoformat(new_start) - timedelta(hours=1))
        alarm = self.w.repo.connection.execute("SELECT remind_at,delivery,status FROM reminders WHERE account_id='member'").fetchone()
        self.assertEqual((datetime.fromisoformat(alarm[0]), alarm[1], alarm[2]),
                         (datetime.fromisoformat(new_start) - timedelta(minutes=90), "ALARM", "SCHEDULED"))

    def test_critical_ladder_is_opt_in_and_never_duplicates_the_preparation_task(self):
        facts = lambda account: {f.task_id: f for f in task_facts(self.w.repo, account, NOW)}  # noqa: E731
        key = f"shared-event:{self.event['id']}"
        self.assertNotIn(key, facts("member"))  # aggressive reminders are never switched on silently
        self.w.ok("member", "group_preferences.update", "group-pi261", {"notification_behavior": "CHANGES_AND_CRITICAL"})
        fact = facts("member")[key]
        self.assertEqual((fact.kind, fact.importance), ("SHARED_EVENT", "CRITICAL"))
        self.w.clock.set(datetime.fromisoformat(self.event["starts_at"]) - timedelta(hours=23))
        decision = decide(fact, None, ReminderPrefs(), self.w.clock.now())
        self.assertEqual((decision.stage.value, decision.reason), ("ESCALATION", "ESCALATE_24H"))
        self.w.clock.set(NOW)
        # With an open preparation task the ladder runs on the task only.
        self.w.ok("member", "task.create", "task-prep-kr2", {"title": "Подготовка", "estimated_total_effort_minutes": 60,
                                                             "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}})
        self.assertNotIn(key, facts("member"))
        self.assertEqual(facts("member")["task-prep-kr2"].importance, "CRITICAL")
        # A personal NORMAL override takes the member off the ladder.
        self.w.ok("other", "group_preferences.update", "group-pi261", {"notification_behavior": "CHANGES_AND_CRITICAL"})
        self.w.ok("other", "shared_event_state.update", self.event["id"], {"criticality_override": "NORMAL"})
        self.assertNotIn(key, facts("other"))

    def test_escalation_messages_go_through_the_existing_engine(self):
        self.w.ok("member", "group_preferences.update", "group-pi261", {"notification_behavior": "CHANGES_AND_CRITICAL"})
        start = datetime.fromisoformat(self.event["starts_at"])
        engine = ReminderEngine.__new__(ReminderEngine)
        at = start - timedelta(hours=5, minutes=50)
        with unittest.mock.patch.object(ReminderPrefs, "quiet_until", return_value=None):
            result = engine._tick(self.w.repo, "member", at)
        rows = [dict(r) for r in self.w.repo.connection.execute(
            "SELECT stage,title FROM reminder_messages WHERE account_id='member' AND stage='ESCALATION'")]
        self.assertEqual(len(rows), 1, (result, rows))
        self.assertIn("Контрольная работа №2", rows[0]["title"])
        with unittest.mock.patch.object(ReminderPrefs, "quiet_until", return_value=None):
            engine._tick(self.w.repo, "member", at + timedelta(minutes=5))
        self.assertEqual(self.w.count("reminder_messages", "account_id='member' AND stage='ESCALATION'"), 1)

    def test_cancellation_keeps_entity_stops_reminders_keeps_preparation(self):
        self.w.ok("member", "task.create", "task-prep-kr2", {"title": "Подготовка", "estimated_total_effort_minutes": 60,
                                                             "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}})
        self.w.ok("member", "shared_event_state.update", self.event["id"], {"remind_before_minutes": 30, "alarm_before_minutes": 45})
        self.w.ok("member", "group_preferences.update", "group-pi261", {"notification_behavior": "CHANGES_AND_CRITICAL"})
        self.w.ok("sched", "shared_event.cancel", self.event["id"], {"expected_version": 1})
        self.assertEqual(self.w.repo.connection.execute("SELECT status FROM shared_events").fetchone()[0], "CANCELLED")
        self.assertNotIn(f"shared-event:{self.event['id']}", [f.task_id for f in task_facts(self.w.repo, "member", NOW)])
        state = self.w.repo.connection.execute("SELECT remind_at FROM reminder_states WHERE task_id=?",
                                               (f"shared-event:{self.event['id']}",)).fetchone()
        self.assertIsNone(state[0])
        self.assertEqual(self.w.repo.connection.execute("SELECT status FROM reminders WHERE account_id='member'").fetchone()[0], "CANCELLED")
        self.assertEqual(self.w.repo.get_task("member", "task-prep-kr2").obligation.lifecycle_status.value, "ACTIVE")
        item = self.w.item("member", self.event["id"])
        self.assertTrue(item["personal"]["suggest_remove_preparation"])
        body = self.w.repo.connection.execute("SELECT title FROM reminder_messages WHERE account_id='member' "
                                              "AND stage LIKE 'GROUP%' ORDER BY created_at DESC, rowid DESC").fetchone()[0]
        self.assertIn("отменена", body)

    def test_change_notice_is_a_diff(self):
        self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "starts_at": T(days=15, hours=3),
                                                                     "location": "R301"})
        row = self.w.repo.connection.execute("SELECT title,body FROM reminder_messages WHERE account_id='member' "
                                             "ORDER BY created_at DESC, rowid DESC").fetchone()
        self.assertIn("перенесена", row["title"])
        self.assertIn("→", row["body"])
        self.assertIn("R201 → R301", row["body"])


class ObligationTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.w.ok("sched", "shared_obligation.create", "sob-lab-0001",
                  {"group_id": "group-pi261", "title": "Сдать лабораторную 3", "obligation_kind": "LAB_REPORT",
                   "deadline": T(days=6), "estimated_effort_hint_minutes": 180})

    def test_no_task_without_consent_no_shared_completion(self):
        self.assertEqual(self.w.count("obligations"), 0)
        self.w.ok("member", "shared_obligation_state.update", "sob-lab-0001", {"acceptance_state": "ACCEPTED"})
        self.assertEqual(self.w.count("obligations"), 0)  # accepting is not creating a task
        refused = self.w.op("member", "shared_obligation_state.update", "sob-lab-0001", {"status": "COMPLETED"})
        self.assertEqual(refused["status"], "REJECTED")
        columns = {r[1] for r in self.w.repo.connection.execute("PRAGMA table_info(shared_obligations)")}
        self.assertNotIn("completed_at", columns)

    def test_take_on_creates_personal_task_that_follows_the_deadline(self):
        task = self.w.ok("member", "task.create", "task-lab-0001", {"title": "Лаба 3", "estimated_total_effort_minutes": 180,
                                                                   "prepares": {"kind": "SHARED_OBLIGATION", "id": "sob-lab-0001"}})
        self.assertEqual(task["actual_cutoff"]["at"], T(days=6))
        self.w.ok("member", "task.complete", "task-lab-0001")
        self.assertEqual(self.w.repo.connection.execute("SELECT status FROM shared_obligations").fetchone()[0], "PUBLISHED")
        item = self.w.item("member", "sob-lab-0001")
        self.assertEqual(item["personal"]["acceptance_state"], "ACCEPTED")
        self.assertEqual(item["personal"]["personal_task"]["status"], "COMPLETED")
        self.assertIsNone(self.w.item("other", "sob-lab-0001")["personal"]["personal_task"])


class OfflineSyncTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def test_personal_override_through_the_offline_queue_and_reconnect_replay(self):
        op = {"op_id": "offline-override-0001", "type": "shared_event_state.update", "entity_id": self.event["id"],
              "payload": {"attendance_override": "OPTIONAL", "criticality_override": "IMPORTANT"}}
        service = SyncService(self.w.repo, account_id="member", principal_id="member", now=NOW, channel="offline-queue")
        first = service.apply_batch([op])[0]
        # Meanwhile the scheduler moves the event (a different owner: no conflict).
        self.w.ok("sched", "shared_event.update", self.event["id"], {"expected_version": 1, "starts_at": T(days=15)})
        again = SyncService(self.w.repo, account_id="member", principal_id="member", now=NOW,
                            channel="offline-queue").apply_batch([op])[0]
        self.assertEqual(first["status"], "APPLIED")
        self.assertTrue(again["replayed"])
        self.assertEqual(self.w.count("user_shared_event_states"), 1)
        self.assertEqual(self.w.count("shared_events"), 1)
        item = self.w.item("member", self.event["id"])
        self.assertEqual((item["attendance"]["effective"], item["criticality"]["effective"], item["version"]),
                         ("OPTIONAL", "IMPORTANT", 2))

    def test_preparation_created_offline_is_one_task_after_reconnect(self):
        op = {"op_id": "offline-prep-0001", "type": "task.create", "entity_id": "task-offline-prep",
              "payload": {"title": "Подготовка", "estimated_total_effort_minutes": 60,
                          "prepares": {"kind": "SHARED_EVENT", "id": self.event["id"]}}}
        for _ in range(3):
            SyncService(self.w.repo, account_id="member", principal_id="member", now=NOW, channel="offline-queue").apply_batch([op])
        self.assertEqual(self.w.count("obligations", "account_id='member'"), 1)

    def test_group_wide_changes_are_not_accepted_from_the_offline_queue(self):
        result = SyncService(self.w.repo, account_id="sched", principal_id="sched", now=NOW, channel="offline-queue").apply_batch([
            {"op_id": "offline-publish-1", "type": "shared_event.cancel", "entity_id": self.event["id"],
             "payload": {"expected_version": 1}}])[0]
        self.assertEqual((result["status"], result["code"]), ("REJECTED", "ONLINE_ONLY"))
        self.assertEqual(self.w.repo.connection.execute("SELECT status FROM shared_events").fetchone()[0], "PUBLISHED")

    def test_personal_change_after_cancellation_and_after_removal(self):
        self.w.ok("sched", "shared_event.cancel", self.event["id"], {"expected_version": 1})
        muted = self.w.op("member", "shared_event_state.update", self.event["id"], {"muted": True})
        self.assertEqual(muted["status"], "APPLIED")  # different owners: no conflict
        version = self.w.repo.connection.execute("SELECT version FROM group_memberships WHERE account_id='member'").fetchone()[0]
        self.w.ok("owner", "membership.update", "group-pi261", {"account_id": "member", "status": "REMOVED", "expected_version": version})
        late = self.w.op("member", "shared_event_state.update", self.event["id"], {"muted": False})
        self.assertEqual(late["code"], "NOT_FOUND")
        self.assertEqual(self.w.count("user_shared_event_states", "account_id='member'"), 1)  # kept, not destroyed


class ExternalBindingTest(unittest.TestCase):
    """An imported class (Google/ICS/any source) annotated by the group, without duplicates."""

    def setUp(self):
        self.w = World()
        self.w.group()
        self.rec = SQLiteReconciliationRepository(self.w.repo)
        self.connectors = SQLiteConnectorRepository(self.w.repo, self.rec)
        for account in ("sched", "member"):
            self.import_series(account)

    def import_series(self, account: str) -> None:
        """Two occurrences of the same weekly seminar, imported into ``account``'s own calendar."""
        source = f"{account}-schedule"
        self.rec.create_source_system(account_id=account, kind="CALENDAR", actor=ActorCategory.CONNECTOR_INGESTION,
                                      source_system_id=source)
        self.connectors.register(account_id=account, connector_id=f"{account}-conn", source_system_id=source,
                                 provider="ics", scope="university:pi-261", connector_version="1")
        for week, day in ((1, 14), (2, 21)):
            occurrence = f"seminar-dm_2026{10 + (day > 20)}{day:02d}"
            event_id = f"{account}-seminar-w{week}"
            self.w.repo.create_fixed_event(account_id=account, title="Дискретная математика · Семинар",
                                           starts_at=NOW + timedelta(days=day, minutes=10),
                                           ends_at=NOW + timedelta(days=day, hours=1, minutes=30),
                                           actor=ActorCategory.CONNECTOR_INGESTION, obligation_id=event_id)
            self.rec.add_source_record(account_id=account, source_system_id=source, actor=ActorCategory.CONNECTOR_INGESTION,
                                       external_entity_id=occurrence, metadata={"recurring_event_id": "seminar-dm"},
                                       source_record_id=f"{account}-rec-{week}")
            self.rec.bind_source_entity(account_id=account, source_system_id=source, external_entity_id=occurrence,
                                        local_entity_id=event_id, match_decision_id="import", actor=ActorCategory.CONNECTOR_INGESTION)

    def annotate(self):
        return self.w.ok("sched", "shared_event.create", "sev-annot-0001",
                         {"group_id": "group-pi261", "title": "Контрольная работа №2", "event_kind": "CONTROL_WORK",
                          "external_local_event_id": "sched-seminar-w1"})

    def test_annotation_binds_one_occurrence_and_the_agenda_shows_one_logical_event(self):
        event = self.annotate()
        binding = event["external_binding"]
        self.assertEqual((binding["external_source_key"], binding["external_event_uid"], binding["external_occurrence_key"]),
                         ("ics:university:pi-261", "seminar-dm", "seminar-dm_20261014"))
        item = self.w.item("member", event["id"])
        self.assertEqual(item["external"]["local_event_id"], "member-seminar-w1")  # the member's own import
        self.assertEqual(item["starts_at"], (NOW + timedelta(days=14, minutes=10)).isoformat())
        # The planner does not count the slot twice.
        self.assertEqual(PersonalProjection(self.w.repo, "member", NOW).busy_constraints(NOW, NOW + timedelta(days=30)), ())
        # The other occurrence of the series is not annotated.
        self.assertFalse(any(i.get("external") and i["external"]["local_event_id"] == "member-seminar-w2"
                             for i in PersonalProjection(self.w.repo, "member", NOW).items()))
        # A member without the import still sees the event at the official time.
        self.assertEqual(self.w.item("other", event["id"])["starts_at"], item["starts_at"])
        # Annotating the same occurrence the same way again is a duplicate, never merged by title.
        duplicate = self.w.op("sched", "shared_event.create", "sev-annot-0002",
                              {"group_id": "group-pi261", "title": "Другое название", "event_kind": "CONTROL_WORK",
                               "external_local_event_id": "sched-seminar-w1"})
        self.assertEqual(duplicate["code"], "POSSIBLE_DUPLICATE")

    def test_group_cannot_change_external_owned_fields(self):
        event = self.annotate()
        for field, value in (("starts_at", T(days=20)), ("location", "R999"), ("timezone", "UTC")):
            refused = self.w.op("sched", "shared_event.update", event["id"], {"expected_version": 1, field: value})
            self.assertEqual(refused["code"], "EXTERNAL_OWNED", field)
        changed = self.w.ok("sched", "shared_event.update", event["id"], {"expected_version": 1, "group_criticality": "IMPORTANT",
                                                                          "description": "Темы 1-4"})
        self.assertEqual(changed["version"], 2)

    def test_official_time_change_keeps_identity_and_overlays(self):
        event = self.annotate()
        self.w.ok("member", "shared_event_state.update", event["id"], {"criticality_override": "IMPORTANT"})
        self.w.ok("member", "task.create", "task-prep-annot", {"title": "Подготовка", "estimated_total_effort_minutes": 60,
                                                               "prepares": {"kind": "SHARED_EVENT", "id": event["id"]}})
        new_start = NOW + timedelta(days=14, hours=2)
        for account in ("sched", "member"):  # the source moved the class; both imports observe it
            current = self.w.repo.get_event(account, f"{account}-seminar-w1")
            self.w.repo.update_fixed_event(account_id=account, obligation_id=f"{account}-seminar-w1",
                                           expected_version=current.obligation.version, starts_at=new_start,
                                           ends_at=new_start + timedelta(minutes=80), actor=ActorCategory.CONNECTOR_INGESTION)
        self.assertEqual(reconcile_external_bindings(self.w.repo, NOW), 1)
        self.assertEqual(reconcile_external_bindings(self.w.repo, NOW), 0)
        self.assertEqual(self.w.count("shared_events"), 1)
        row = self.w.repo.connection.execute("SELECT * FROM shared_events").fetchone()
        self.assertEqual((row["id"], row["version"], row["event_kind"], row["title"]),
                         (event["id"], 2, "CONTROL_WORK", "Контрольная работа №2"))
        item = self.w.item("member", event["id"])
        self.assertEqual(item["criticality"]["effective"], "IMPORTANT")
        self.assertEqual(self.w.repo.get_task("member", "task-prep-annot").actual_cutoff.at, new_start)
        self.assertEqual(self.w.item("other", event["id"])["starts_at"], new_start.isoformat())
        audit = self.w.repo.connection.execute("SELECT actor_account_id FROM group_audit WHERE action='EXTERNAL_CHANGE'").fetchone()[0]
        self.assertEqual(audit, "external:ics:university:pi-261")

    def test_detach_keeps_external_event_and_overlays(self):
        event = self.annotate()
        self.w.ok("member", "shared_event_state.update", event["id"], {"attendance_override": "OPTIONAL"})
        self.w.ok("sched", "shared_event.detach_external", event["id"], {"expected_version": 1})
        self.assertEqual(self.w.count("external_event_bindings", "status='DETACHED'"), 1)
        self.assertEqual(self.w.count("obligations", "id IN ('sched-seminar-w1','member-seminar-w1')"), 2)
        self.assertEqual(self.w.item("member", event["id"])["attendance"]["effective"], "OPTIONAL")

    def test_an_event_that_was_not_imported_cannot_be_bound(self):
        self.w.repo.create_fixed_event(account_id="sched", title="Своё", starts_at=NOW + timedelta(days=1),
                                       ends_at=NOW + timedelta(days=1, hours=1), actor=ActorCategory.USER_UI,
                                       obligation_id="sched-own-event")
        refused = self.w.op("sched", "shared_event.create", "sev-annot-0003",
                            {"group_id": "group-pi261", "title": "x", "external_local_event_id": "sched-own-event"})
        self.assertIn("not imported", refused["message"])
        # Nor someone else's import (IDOR): identity is resolved in the caller's own account only.
        refused = self.w.op("sched", "shared_event.create", "sev-annot-0004",
                            {"group_id": "group-pi261", "title": "x", "external_local_event_id": "member-seminar-w1"})
        self.assertEqual(refused["status"], "REJECTED")


class AnnouncementTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.group()
        self.announcement = self.w.ok("sched", "announcement.create", "san-news-0001",
                                      {"group_id": "group-pi261", "title": "Пара переносится в Zoom", "body": "Ссылку пришлю утром",
                                       "importance": "IMPORTANT"})

    def test_announcement_is_informational_not_an_event_or_task(self):
        item = self.w.item("member", self.announcement["id"])
        self.assertEqual(item["kind"], "ANNOUNCEMENT")
        self.assertNotIn("starts_at", item)
        self.assertNotIn("deadline", item)
        self.assertEqual(self.w.count("shared_events") + self.w.count("shared_obligations") + self.w.count("obligations"), 0)
        self.assertTrue(item["visible_in_updates"])
        self.assertFalse(item["visible_in_agenda"])  # only when the member turns it on
        self.w.ok("member", "group_preferences.update", "group-pi261", {"announcements_in_agenda": True})
        self.assertTrue(self.w.item("member", self.announcement["id"])["visible_in_agenda"])
        self.assertEqual(self.w.count("reminder_messages", "account_id='member' AND stage='GROUP_UPDATE'"), 1)

    def test_subscription_and_mute(self):
        self.w.ok("other", "group_preferences.update", "group-pi261", {"show_announcements": False})
        self.w.ok("member", "group_preferences.update", "group-pi261", {"muted": True})
        self.w.ok("sched", "announcement.create", "san-news-0002", {"group_id": "group-pi261", "title": "Ещё", "body": "…"})
        self.assertEqual(self.w.count("reminder_messages", "account_id IN ('other','member') AND task_ids_json LIKE '%san-news-0002%'"), 0)
        self.assertEqual(self.w.count("reminder_messages", "account_id='owner' AND task_ids_json LIKE '%san-news-0002%'"), 1)
        self.assertFalse(self.w.item("other", "san-news-0002")["visible_in_updates"])
        # Mute removes nothing.
        self.assertEqual(self.w.count("group_memberships", "account_id='member' AND status='ACTIVE'"), 1)

    def test_retract_keeps_the_entity_and_audit_and_stops_pending_notices(self):
        pending = self.w.count("reminder_messages", "delivery_state='PENDING' AND task_ids_json LIKE '%san-news-0001%'")
        self.assertGreater(pending, 0)
        self.w.ok("sched", "announcement.retract", self.announcement["id"], {"expected_version": 1})
        self.assertEqual(self.w.count("shared_announcements", "status='RETRACTED'"), 1)
        self.assertEqual(self.w.count("reminder_messages", "delivery_state='PENDING' AND task_ids_json LIKE '%san-news-0001%'"), 0)
        self.assertEqual([r[0] for r in self.w.repo.connection.execute(
            "SELECT action FROM group_audit WHERE entity_id='san-news-0001' ORDER BY id")], ["CREATE", "RETRACT"])


class AssistantActionsTest(unittest.TestCase):
    """Typed group actions through the existing Assistant boundary: preview, confirmation, same operations."""

    def setUp(self):
        self.w = World()
        self.w.group()
        self.event = self.w.control_work()

    def service(self, account, actions):
        from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService

        class Stub:
            name = "stub-llm"

            def interpret(self, text, context):
                Stub.context = context
                return {"message": "ok", "actions": actions}

        service = SQLiteAssistantService(self.w.repo, AuthenticatedPrincipal(account, account, "test"), Stub())
        return service, Stub

    @staticmethod
    def action(command, payload, expected=None, confirm=False):
        return {"command": command, "payload": payload, "confidence": 0.9, "unresolved_fields": [],
                "expected_version": expected, "requires_confirmation": confirm}

    def test_group_wide_action_is_never_applied_without_confirmation(self):
        from student_execution_os.domain.errors import AuthorizationDenied
        service, stub = self.service("sched", [self.action("UPDATE_SHARED_EVENT",
                                                           {"shared_event_id": self.event["id"], "starts_at": T(days=14, hours=2)},
                                                           expected=1, confirm=False)])
        preview = service.interpret("перенеси контрольную группы на 14:00")
        self.assertIn(self.event["id"], [i["id"] for i in stub.context["shared_items"]])
        action = preview["actions"][0]
        self.assertTrue(action["requires_confirmation"])  # forced, whatever the model said
        self.assertEqual(self.w.count("group_audit", "action='UPDATE'"), 0)
        with self.assertRaises(AuthorizationDenied):
            service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "k-1"})
        result = service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]], "idempotency_key": "k-2",
                                "confirmed_action_ids": [action["id"]]})
        self.assertEqual(result["results"][0]["operation"], "shared_event.update")
        row = self.w.repo.connection.execute("SELECT version,starts_at FROM shared_events").fetchone()
        self.assertEqual((row[0], row[1]), (2, T(days=14, hours=2)))
        self.assertEqual(self.w.repo.connection.execute("SELECT mutation_id FROM group_audit WHERE action='UPDATE'").fetchone()[0],
                         f"assistant:{action['id']}")

    def test_member_cannot_be_talked_into_publishing_and_personal_actions_work(self):
        from student_execution_os.domain.errors import ValidationError
        service, _ = self.service("member", [self.action("CREATE_SHARED_EVENT", {
            "group_id": "group-pi261", "title": "Квиз", "event_kind": "QUIZ", "starts_at": T(days=3), "ends_at": T(days=3, hours=1)})])
        with self.assertRaises(ValidationError):
            service.interpret("добавь в ПИ-261 квиз")
        service, _ = self.service("member", [
            self.action("CREATE_GROUP_PROPOSAL", {"group_id": "group-pi261", "proposal": {
                "kind": "SHARED_EVENT", "title": "Квиз по АиСД", "event_kind": "QUIZ", "starts_at": T(days=3), "ends_at": T(days=3, hours=1)}}),
            self.action("SET_PERSONAL_EVENT_PREFERENCES", {"shared_event_id": self.event["id"], "attendance_override": "OPTIONAL"}),
            self.action("CREATE_PREPARATION_TASK", {"shared_event_id": self.event["id"], "estimated_total_effort_minutes": 120}),
        ])
        preview = service.interpret("предложи группе квиз; для меня это необязательно; создай подготовку")
        flags = [a["requires_confirmation"] for a in preview["actions"]]
        self.assertEqual(flags, [True, False, False])
        ids = [a["id"] for a in preview["actions"]]
        service.apply({"batch_id": preview["batch_id"], "action_ids": ids, "idempotency_key": "k-3", "confirmed_action_ids": ids[:1]})
        self.assertEqual(self.w.count("group_proposals", "status='PENDING'"), 1)
        self.assertEqual(self.w.count("shared_events"), 1)  # a proposal publishes nothing
        item = self.w.item("member", self.event["id"])
        self.assertEqual(item["attendance"]["effective"], "OPTIONAL")
        self.assertEqual(item["personal"]["preparation_task"]["title"], "Контрольная работа №2")
        # An outsider's model cannot reach the group at all.
        service, _ = self.service("out", [self.action("SET_PERSONAL_EVENT_PREFERENCES", {"shared_event_id": self.event["id"], "muted": True})])
        with self.assertRaises(ValidationError):
            service.interpret("скрой")


class LifecycleTest(unittest.TestCase):
    def test_account_deletion_hands_over_the_group_and_keeps_its_facts(self):
        import tempfile
        from pathlib import Path
        from student_execution_os.reliability import SQLiteDataLifecycle
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "groups.sqlite")
            w = World(db)
            w.group()
            event = w.control_work()
            w.ok("member", "shared_event_state.update", event["id"], {"muted": True})
            export = SQLiteDataLifecycle(db, now=lambda: NOW).export_account("member").to_dict()
            self.assertEqual(len(export["tables"]["user_shared_event_states"]), 1)
            self.assertNotIn("shared_events", export["tables"])  # group-owned, not one member's data
            revision = w.repo.get_server_revision("owner")
            w.repo.close()
            SQLiteDataLifecycle(db, now=lambda: NOW).delete_account("owner", expected_server_revision=revision,
                                                                    confirm_account_id="owner")
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                owner = repo.connection.execute("SELECT owner_account_id,status FROM groups").fetchone()
                self.assertEqual(tuple(owner), ("sched", "ACTIVE"))  # the scheduler is the next in line
                self.assertEqual(repo.connection.execute(
                    "SELECT role FROM group_memberships WHERE account_id='sched'").fetchone()[0], "OWNER")
                self.assertEqual(repo.connection.execute("SELECT count(*) FROM shared_events").fetchone()[0], 1)
                self.assertEqual(repo.connection.execute(
                    "SELECT count(*) FROM group_audit WHERE actor_account_id='owner'").fetchone()[0], 0)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
