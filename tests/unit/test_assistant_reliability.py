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
        for reason, delivery in (("NETWORK", "NOT_SENT"), ("UPSTREAM", "ANSWERED")):
            with self.subTest(reason=reason):
                calls = 0

                def request():
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise ProviderUnavailable("private failure", reason, delivery=delivery)
                    return "ok"

                trace = ReliabilityTrace()
                result = ReliabilityPolicy(base_delay_seconds=0).run(request, trace=trace)
                self.assertEqual((result, calls, trace.retries), ("ok", 2, 1))

    def test_rate_limit_retries_once_when_provider_wait_fits_the_operation_budget(self):
        for retry_after, expected in ((1, 2), (18, 2), (30, 2), (31, 1), (None, 1)):
            with self.subTest(retry_after=retry_after):
                calls = 0

                def request():
                    nonlocal calls
                    calls += 1
                    raise ProviderUnavailable("rate limited", "RATE_LIMITED", 429, retry_after=retry_after,
                                              delivery="ANSWERED")

                with self.assertRaises(ProviderUnavailable):
                    ReliabilityPolicy().run(request, trace=ReliabilityTrace(), sleep=lambda _: None)
                self.assertEqual(calls, expected)

    def test_rate_limit_recovers_after_one_provider_advised_wait(self):
        calls = 0
        waits = []

        def request():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderUnavailable("rate limited", "RATE_LIMITED", 429, retry_after=18,
                                          delivery="ANSWERED")
            return "ok"

        trace = ReliabilityTrace()
        result = ReliabilityPolicy().run(request, trace=trace, sleep=waits.append)
        self.assertEqual((result, calls, trace.retries, waits), ("ok", 2, 1, [18.0]))

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
                metric_names = {
                    row["metric_name"]
                    for row in repository.connection.execute(
                        "SELECT metric_name FROM operational_metrics WHERE account_id='account'"
                    )
                }
        self.assertEqual(provider.feedback, "UNKNOWN_FIELD")
        self.assertEqual(result["reliability"], {
            "attempts": 2, "retries": 0, "repair_attempted": True, "repair_succeeded": True, "retry_stop": None,
        })
        self.assertEqual(result["actions"][0]["provenance"]["fields"]["title"], "MODEL_EXPLICIT")
        self.assertIn("assistant_interpretation_count", metric_names)
        self.assertIn("assistant_interpretation_latency_ms", metric_names)
        self.assertIn("assistant_repair_attempt_count", metric_names)
        self.assertIn("assistant_structured_output_failure_count", metric_names)

    def test_provider_failure_and_local_fallback_have_payload_free_metrics(self):
        class Provider:
            name = "network-fixture"
            model = "fixture"

            def interpret(self, text, context):
                raise ProviderUnavailable("private upstream detail", "NETWORK")

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "fallback.sqlite"),
                clock=FrozenClock(datetime(2026, 10, 2, tzinfo=timezone.utc)),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                result = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"), provider=Provider(),
                    reliability_policy=ReliabilityPolicy(base_delay_seconds=0),
                ).interpret("Задача: Секретный отчёт, 15 min")
                rows = repository.connection.execute(
                    "SELECT metric_name,dimensions_json FROM operational_metrics WHERE account_id='account'"
                ).fetchall()
        self.assertEqual((result["engine"], result["fallback_reason"]), ("LOCAL", "NETWORK"))
        names = {row["metric_name"] for row in rows}
        self.assertIn("assistant_provider_failure_count", names)
        self.assertIn("assistant_local_fallback_count", names)
        self.assertNotIn("Секретный", str([(row["metric_name"], row["dimensions_json"]) for row in rows]))


class TimeoutDecisionMatrixTest(unittest.TestCase):
    """The complete retry matrix: only a failure that cannot have produced a billed generation retries."""

    MATRIX = [
        # reason, delivery, http_status, retry_after, should_retry, expected decision
        ("NETWORK", "NOT_SENT", None, None, None, "RETRY"),            # connect / pool / write failure
        ("NETWORK", "UNKNOWN", None, None, None, "UNKNOWN_OUTCOME"),   # dropped after sending
        ("TIMEOUT", "UNKNOWN", None, None, None, "UNKNOWN_OUTCOME"),   # read timeout: may be generating
        ("TIMEOUT", "NOT_SENT", None, None, None, "BUDGET_EXHAUSTED"), # refused locally, no time left
        ("UPSTREAM", "ANSWERED", 503, None, None, "RETRY"),            # provider says it failed
        ("UPSTREAM", "ANSWERED", 500, None, False, "PROVIDER_SAID_NO"),
        ("UPSTREAM", "UNKNOWN", 504, None, None, "UNKNOWN_OUTCOME"),   # gateway timeout
        ("UPSTREAM", "UNKNOWN", 502, None, True, "UNKNOWN_OUTCOME"),   # advice cannot undo a possible generation
        ("RATE_LIMITED", "ANSWERED", 429, 1, None, "RETRY"),
        ("RATE_LIMITED", "ANSWERED", 429, 18, None, "RETRY"),
        ("RATE_LIMITED", "ANSWERED", 429, 30, None, "RETRY"),
        ("RATE_LIMITED", "ANSWERED", 429, 31, None, "RETRY_AFTER_TOO_LONG"),
        ("RATE_LIMITED", "ANSWERED", 429, None, None, "RETRY_AFTER_TOO_LONG"),
        ("QUOTA", "ANSWERED", 429, None, None, "NOT_TRANSIENT"),
        ("AUTH", "ANSWERED", 401, None, None, "NOT_TRANSIENT"),
        ("SERVER_BLOCKED", "ANSWERED", 403, None, None, "NOT_TRANSIENT"),
        ("FORMAT", "ANSWERED", 400, None, None, "NOT_TRANSIENT"),
        ("NOT_FOUND", "ANSWERED", 404, None, None, "NOT_TRANSIENT"),
        ("REQUEST", None, None, None, None, "NOT_TRANSIENT"),
    ]

    def test_decision_matrix(self):
        policy = ReliabilityPolicy()
        for reason, delivery, status, retry_after, should_retry, expected in self.MATRIX:
            with self.subTest(reason=reason, delivery=delivery, status=status, retry_after=retry_after):
                exc = ProviderUnavailable("x", reason, status, retry_after=retry_after, delivery=delivery,
                                          should_retry=should_retry)
                self.assertEqual(policy.retry_decision(exc, ReliabilityTrace(), remaining=40.0), expected)

    def test_retry_is_skipped_when_the_budget_cannot_fit_another_call(self):
        exc = ProviderUnavailable("x", "UPSTREAM", 503, delivery="ANSWERED")
        self.assertEqual(ReliabilityPolicy().retry_decision(exc, ReliabilityTrace(), remaining=2.0), "BUDGET_EXHAUSTED")
        exc = ProviderUnavailable("x", "RATE_LIMITED", 429, retry_after=2, delivery="ANSWERED")
        self.assertEqual(ReliabilityPolicy().retry_decision(exc, ReliabilityTrace(), remaining=4.0), "BUDGET_EXHAUSTED")

    def test_total_attempts_are_capped_across_primary_and_repair_calls(self):
        clock = [0.0]
        calls = 0

        def failing():
            nonlocal calls
            calls += 1
            raise ProviderUnavailable("x", "NETWORK", delivery="NOT_SENT")

        trace = ReliabilityTrace()
        policy = ReliabilityPolicy(base_delay_seconds=0)
        with self.assertRaises(ProviderUnavailable):
            policy.run(failing, trace=trace, clock=lambda: clock[0])
        self.assertEqual((calls, trace.retries), (2, 1))
        # A later call in the same operation (the structured-output repair) shares the trace:
        with self.assertRaises(ProviderUnavailable):
            policy.run(failing, trace=trace, clock=lambda: clock[0])
        with self.assertRaises(ProviderUnavailable) as stopped:
            policy.run(failing, trace=trace, clock=lambda: clock[0])
        self.assertEqual(calls, 3)  # max_attempts, no matter how many runs are started
        self.assertEqual(stopped.exception.reason, "TIMEOUT")
        self.assertEqual(trace.stop_reason, "ATTEMPTS_EXHAUSTED")

    def test_every_call_runs_under_the_operation_deadline(self):
        from student_execution_os.agent.providers import CALL_DEADLINE, _bounded_timeout

        seen = []
        clock = [100.0]

        def call():
            seen.append(CALL_DEADLINE.get())
            return "ok"

        trace = ReliabilityTrace()
        ReliabilityPolicy(total_budget_seconds=45).run(call, trace=trace, clock=lambda: clock[0])
        self.assertEqual(seen, [145.0])
        self.assertIsNone(CALL_DEADLINE.get())  # reset after the call
        token = CALL_DEADLINE.set(__import__("time").monotonic() + 5)
        try:
            self.assertLessEqual(_bounded_timeout(30.0), 5.0)
        finally:
            CALL_DEADLINE.reset(token)
        token = CALL_DEADLINE.set(__import__("time").monotonic() - 1)
        try:
            with self.assertRaises(ProviderUnavailable) as refused:
                _bounded_timeout(30.0)
            self.assertEqual((refused.exception.reason, refused.exception.delivery), ("TIMEOUT", "NOT_SENT"))
        finally:
            CALL_DEADLINE.reset(token)

    def test_exhausted_budget_stops_before_calling(self):
        clock = [0.0]
        calls = 0

        def slow_failure():
            nonlocal calls
            calls += 1
            clock[0] += 50  # the call used the whole budget
            raise ProviderUnavailable("x", "UPSTREAM", 503, delivery="ANSWERED")

        trace = ReliabilityTrace()
        with self.assertRaises(ProviderUnavailable):
            ReliabilityPolicy(base_delay_seconds=0).run(slow_failure, trace=trace, clock=lambda: clock[0])
        self.assertEqual((calls, trace.stop_reason), (1, "BUDGET_EXHAUSTED"))

    def test_http_failures_are_classified_by_delivery(self):
        import httpx
        from unittest import mock

        from student_execution_os.agent import providers

        cases = [
            (httpx.ConnectTimeout("t"), "NETWORK", "NOT_SENT"),
            (httpx.PoolTimeout("t"), "NETWORK", "NOT_SENT"),
            (httpx.WriteTimeout("t"), "NETWORK", "NOT_SENT"),
            (httpx.ConnectError("refused"), "NETWORK", "NOT_SENT"),
            (httpx.ReadTimeout("t"), "TIMEOUT", "UNKNOWN"),
            (httpx.ReadError("reset"), "NETWORK", "UNKNOWN"),
            (httpx.RemoteProtocolError("closed"), "NETWORK", "UNKNOWN"),
        ]
        for error, reason, delivery in cases:
            with self.subTest(error=type(error).__name__), mock.patch.object(providers.httpx, "post", side_effect=error):
                with self.assertRaises(ProviderUnavailable) as caught:
                    providers._post("https://api.example.test/v1/chat", headers={}, body={}, timeout=5, name="t")
                self.assertEqual((caught.exception.reason, caught.exception.delivery), (reason, delivery))
        for status, delivery, header, advice in ((503, "ANSWERED", "true", True), (500, "ANSWERED", "false", False),
                                                 (504, "UNKNOWN", None, None), (502, "UNKNOWN", None, None)):
            response = httpx.Response(status, headers={"x-should-retry": header} if header else {},
                                      request=httpx.Request("POST", "https://api.example.test/v1/chat"))
            with self.subTest(status=status), mock.patch.object(providers.httpx, "post", return_value=response):
                with self.assertRaises(ProviderUnavailable) as caught:
                    providers._post("https://api.example.test/v1/chat", headers={}, body={}, timeout=5, name="t")
                self.assertEqual((caught.exception.delivery, caught.exception.should_retry), (delivery, advice))

    def test_unknown_outcome_degrades_to_local_parser_without_a_second_paid_call(self):
        class Provider:
            name = "slow-fixture"
            model = "fixture"
            calls = 0

            def interpret(self, text, context):
                Provider.calls += 1
                raise ProviderUnavailable("did not answer in time", "TIMEOUT", delivery="UNKNOWN")

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "timeout.sqlite"),
                clock=FrozenClock(datetime(2026, 10, 2, tzinfo=timezone.utc)),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                result = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"), provider=Provider(),
                ).interpret("Купить молоко завтра")
                decisions = [
                    row["dimensions_json"] for row in repository.connection.execute(
                        "SELECT dimensions_json FROM operational_metrics WHERE metric_name='assistant_retry_stop_count'")
                ]
        self.assertEqual(Provider.calls, 1)
        self.assertEqual((result["engine"], result["fallback_reason"]), ("LOCAL", "TIMEOUT"))
        self.assertEqual(result["reliability"]["retry_stop"], "UNKNOWN_OUTCOME")
        self.assertTrue(any("UNKNOWN_OUTCOME" in item for item in decisions))



class FormatDowngradeTest(unittest.TestCase):
    def refused(self):
        return ProviderUnavailable("refused", "FORMAT", 400, delivery="ANSWERED", format_downgraded=True)

    def test_downgrade_is_one_bounded_attempt_not_a_transient_retry(self):
        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise self.refused()
            return "ok"

        trace = ReliabilityTrace()
        self.assertEqual(ReliabilityPolicy().run(call, trace=trace, sleep=lambda _s: None), "ok")
        self.assertEqual((trace.attempts, trace.retries, trace.format_downgrades), (2, 0, 1))

    def test_a_provider_that_keeps_claiming_downgrade_cannot_loop(self):
        calls = []

        def call():
            calls.append(1)
            raise self.refused()

        trace = ReliabilityTrace()
        with self.assertRaises(ProviderUnavailable):
            ReliabilityPolicy().run(call, trace=trace, sleep=lambda _s: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual((trace.format_downgrades, trace.stop_reason), (1, "RETRIES_EXHAUSTED"))

    def test_downgrade_then_rate_limit_keeps_one_retry_and_the_attempt_cap(self):
        outcomes = [self.refused(),
                    ProviderUnavailable("limited", "RATE_LIMITED", 429, retry_after=1, delivery="ANSWERED"),
                    ProviderUnavailable("limited", "RATE_LIMITED", 429, retry_after=1, delivery="ANSWERED")]
        sleeps = []

        def call():
            raise outcomes.pop(0)

        trace = ReliabilityTrace()
        with self.assertRaises(ProviderUnavailable) as caught:
            ReliabilityPolicy().run(call, trace=trace, sleep=sleeps.append)
        self.assertEqual(caught.exception.reason, "RATE_LIMITED")
        self.assertEqual((trace.attempts, trace.retries, trace.format_downgrades), (3, 1, 1))
        self.assertEqual(sleeps, [1.0])  # exactly the provider-advised wait, once

    def test_downgrade_then_repair_is_bounded_by_the_operation(self):
        class Provider:
            name = "fixture"
            model = "fixture"

            def __init__(self):
                self.calls = 0

            def interpret(self, text, context):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderUnavailable("refused", "FORMAT", 400, delivery="ANSWERED", format_downgraded=True)
                raise ProviderUnavailable("not json", "FORMAT")

            def repair(self, text, context, feedback):
                self.calls += 1
                raise ProviderUnavailable("refused", "FORMAT", 400, delivery="ANSWERED", format_downgraded=True)

        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "downgrade.sqlite"),
                clock=FrozenClock(datetime(2026, 10, 2, tzinfo=timezone.utc)),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                provider = Provider()
                result = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"), provider=provider,
                ).interpret("Essay", degrade_invalid=True)
                downgrades = repository.connection.execute(
                    "SELECT COUNT(*) FROM operational_metrics WHERE metric_name='assistant_format_downgrade_count'"
                ).fetchone()[0]
        # interpret: schema refused -> one JSON-mode re-send -> FORMAT -> one repair;
        # the repair's own downgrade claim is not honoured a second time.
        self.assertEqual(provider.calls, 3)
        self.assertEqual((result["engine"], result["fallback_reason"]), ("LOCAL", "FORMAT"))
        self.assertEqual(result["reliability"]["attempts"], 3)
        self.assertEqual(downgrades, 1)

if __name__ == "__main__":
    unittest.main()
