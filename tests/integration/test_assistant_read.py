from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.agent.providers import ProviderUnavailable
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class ReadProvider:
    name = "read-fixture"
    model = "fixture"

    def interpret(self, text, context):
        if text == "mixed":
            return {"message": "invalid", "read_query": {"kind": "WHAT_NOW"}, "actions": [{
                "command": "CREATE_TASK", "payload": {"title": "No"}, "confidence": 1,
                "unresolved_fields": [], "expected_version": None, "requires_confirmation": False,
            }]}
        return {"message": "facts", "actions": [], "read_query": {
            "kind": "AGENDA_WINDOW",
            "starts_at": "2026-10-02T09:00:00+00:00",
            "ends_at": "2026-10-03T09:00:00+00:00",
        }}


class AssistantReadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(
            str(Path(self.temporary.name) / "read.sqlite"), clock=FrozenClock(NOW),
        )
        self.repository.initialize()
        self.repository.create_account("account")
        self.repository.create_account("other")
        self.repository.create_event(
            account_id="account", obligation_id="class", title="Пара",
            description=None, category=ObligationCategory.LESSON, importance=Importance.NORMAL,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 2, 13, 30, tzinfo=timezone.utc),
            attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
        )
        self.repository.create_event(
            account_id="other", obligation_id="private", title="Private",
            description=None, category=ObligationCategory.MEETING, importance=Importance.NORMAL,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
            attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
        )
        self.service = SQLiteAssistantService(
            self.repository, AuthenticatedPrincipal("account", "user", "client"), provider=ReadProvider(),
        )

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def test_read_query_returns_canonical_facts_without_preview_or_mutation(self):
        revision = self.repository.get_server_revision("account")
        result = self.service.interpret("Что у меня сегодня?")
        self.assertIsNone(result["batch_id"])
        self.assertEqual(result["actions"], [])
        self.assertFalse(result["read"]["mutated_canonical_state"])
        self.assertEqual([item["title"] for item in result["read"]["facts"]], ["Пара"])
        self.assertEqual(self.repository.get_server_revision("account"), revision)
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT count(*) FROM assistant_batches WHERE account_id='account'"
            ).fetchone()[0],
            0,
        )

    def test_read_and_mutation_cannot_be_mixed(self):
        with self.assertRaises(ValidationError):
            self.service.interpret("mixed")

    def test_read_target_cannot_cross_account_scope(self):
        class CrossAccountProvider:
            name = "cross-account-fixture"
            model = "fixture"

            def interpret(self, text, context):
                return {"message": "bad", "actions": [], "read_query": {
                    "kind": "ITEM_LOOKUP", "obligation_id": "private",
                }}

        with self.assertRaises(ValidationError):
            SQLiteAssistantService(
                self.repository, AuthenticatedPrincipal("account", "user", "client"),
                provider=CrossAccountProvider(),
            ).interpret("Когда private?")

    def test_provider_failure_does_not_turn_read_question_into_a_task(self):
        class OfflineProvider:
            name = "offline-fixture"
            model = "fixture"

            def interpret(self, text, context):
                raise ProviderUnavailable("offline", "NETWORK")

        before = self.repository.connection.execute(
            "SELECT count(*) FROM obligations WHERE account_id='account'"
        ).fetchone()[0]
        with self.assertRaises(ValidationError):
            SQLiteAssistantService(
                self.repository, AuthenticatedPrincipal("account", "user", "client"),
                provider=OfflineProvider(),
            ).interpret("Что у меня сегодня?")
        after = self.repository.connection.execute(
            "SELECT count(*) FROM obligations WHERE account_id='account'"
        ).fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
