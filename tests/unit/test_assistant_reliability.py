from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.agent.providers import ProviderUnavailable
from student_execution_os.agent.reliability import ReliabilityPolicy, ReliabilityTrace, sanitized_validation_feedback
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence import SQLiteCanonicalRepository


class AssistantReliabilityTest(unittest.TestCase):
    def test_network_and_upstream_retry_once_without_storm(self):
        for reason in ("NETWORK", "UPSTREAM"):
            with self.subTest(reason=reason):
                calls = 0

                def request():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise ProviderUnavailable("private failure", reason)
                    return "ok"

                trace = ReliabilityTrace()
                result = ReliabilityPolicy(base_delay_seconds=0).run(request, trace=trace)
                self.assertEqual((result, calls, trace.retries), ("ok", 2, 1))

    def test_rate_limit_retries_only_with_short_explicit_retry_after(self):
        for retry_after, expected in ((1, 2), (7, 1), (None, 1)):
            with self.subTest(retry_after=retry_after):
                calls = 0

                def request():
                    nonlocal calls
                    calls += 1
                    raise ProviderUnavailable("rate limited", "RATE_LIMITED", 429, retry_after=retry_after)

                with self.assertRaises(ProviderUnavailable):
                    ReliabilityPolicy().run(request, trace=ReliabilityTrace(), sleep=lambda _: None)
                self.assertEqual(calls, expected)

    def test_timeout_auth_and_format_are_not_transient_retries(self):
        for reason in ("TIMEOUT", "AUTH", "FORMAT"):
            calls = 0

            def request():
                nonlocal calls
                calls += 1
                raise ProviderUnavailable("failure", reason)

            with self.assertRaises(ProviderUnavailable):
                ReliabilityPolicy(base_delay_seconds=0).run(request, trace=ReliabilityTrace())
            self.assertEqual(calls, 1)

    def test_validator_feedback_is_sanitized_to_stable_codes(self):
        feedback = sanitized_validation_feedback(ValidationError("database row 42 has unsupported fields: secret"))
        self.assertEqual(feedback, "UNKNOWN_FIELD")
        self.assertNotIn("secret", feedback)

    def test_one_repair_can_replace_invalid_model_action(self):
        class Provider:
            name = "repair-fixture"
            model = "fixture"

            def interpret(self, text, context):
                return {"message": "bad", "actions": [{
                    "command": "CREATE_TASK", "payload": {"title": "Essay", "invented": "no"},
                    "confidence": 0.9, "unresolved_fields": [], "expected_version": None,
                    "requires_confirmation": False,
                }]}

            def repair(self, text, context, feedback):
                self.feedback = feedback
                return {"message": "fixed", "actions": [{
                    "command": "CREATE_TASK", "payload": {"title": "Essay"},
                    "confidence": 0.9, "unresolved_fields": ["estimated_total_effort_minutes"],
                    "expected_version": None, "requires_confirmation": False,
                    "field_provenance": {"title": "MODEL_EXPLICIT"},
                }]}

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "repair.sqlite"),
                clock=FrozenClock(datetime(2026, 10, 2, tzinfo=timezone.utc)),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                provider = Provider()
                result = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"), provider=provider,
                ).interpret("Essay")
        self.assertEqual(provider.feedback, "UNKNOWN_FIELD")
        self.assertEqual(result["reliability"], {
            "attempts": 2, "retries": 0, "repair_attempted": True, "repair_succeeded": True,
        })
        self.assertEqual(result["actions"][0]["provenance"]["fields"]["title"], "MODEL_EXPLICIT")


if __name__ == "__main__":
    unittest.main()
