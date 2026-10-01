"""R5: provider failure classification, deterministic egress and secret redaction.

Everything here is MOCKED at the HTTP or socket layer; see the RC ledger for the
separately labelled production-like and live evidence.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import tempfile
import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from student_execution_os.agent.smoke import platform_llm_smoke
from student_execution_os.agent.providers import (
    PlatformProviderPool,
    ProviderUnavailable,
    _reason,
    _retry_after,
    assert_public_base_url,
    build_provider,
)

GROQ = "https://api.groq.com/openai/v1"
KEY = "gsk_live_secret_value_1234567890abcdef"
RELAY_TOKEN = "relay-token-" + "r" * 40
PROMPT = "секретный план студента"
TYPED = '{"message":"ok","actions":[{"command":"CREATE_TASK","payload":{"title":"Эссе"},"confidence":0.9,' \
        '"unresolved_fields":[],"expected_version":null,"requires_confirmation":false}]}'


def _addr(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


def _ok(content: object = TYPED, **extra) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}, **extra}


class _Transport(httpx.BaseTransport):
    """An httpx transport standing in for Groq (or the relay)."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.handler(request)
        if isinstance(result, Exception):
            raise result
        return result


class ProviderMatrixTests(unittest.TestCase):
    """Platform-shaped Groq provider (operator base URL, DIRECT route)."""

    def setUp(self) -> None:
        self.logs: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: self.logs.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger("student_execution_os.llm")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def run_with(self, handler, provider=None):
        transport = _Transport(handler)
        real_client = httpx.Client

        def post(url, **kwargs):
            self.assertIs(kwargs.pop("trust_env"), False)
            with real_client(transport=transport, trust_env=False, follow_redirects=kwargs.pop("follow_redirects"),
                             timeout=kwargs.pop("timeout")) as client:
                return client.post(url, **kwargs)

        provider = provider or build_provider("openai-compatible", api_key=KEY, model="openai/gpt-oss-20b",
                                              base_url=GROQ)
        with patch("student_execution_os.agent.providers.httpx.post", side_effect=post):
            try:
                return provider.interpret(PROMPT, {"now": "2026-09-28T09:00:00+00:00"}), transport, provider
            except ProviderUnavailable as exc:
                return exc, transport, provider

    def assert_clean(self, *texts: str) -> None:
        for text in (*texts, *self.logs):
            for secret in (KEY, RELAY_TOKEN, PROMPT):
                self.assertNotIn(secret, text)
        for line in self.logs:
            self.assertNotIn("api.groq.com", line)  # no URL in operator logs either

    def failure(self, handler) -> ProviderUnavailable:
        result, _transport, _provider = self.run_with(handler)
        self.assertIsInstance(result, ProviderUnavailable, result)
        self.assert_clean(str(result), repr(result))
        return result

    def test_success_is_a_structured_typed_answer_with_usage(self):
        result, transport, provider = self.run_with(lambda r: httpx.Response(200, json=_ok()))
        self.assertEqual(result["actions"][0]["command"], "CREATE_TASK")
        self.assertEqual(provider.last_usage, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        request = transport.requests[0]
        self.assertEqual(str(request.url), GROQ + "/chat/completions")
        self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
        self.assertIn("route=DIRECT result=OK http_status=200", self.logs[-1])
        self.assert_clean(repr(provider))

    def test_http_failure_classes(self):
        cases = [
            (401, {"error": {"message": "Invalid API Key", "type": "invalid_request_error",
                             "code": "invalid_api_key"}}, "AUTH"),
            (403, {"error": {"message": "Forbidden"}}, "SERVER_BLOCKED"),
            (403, "<html>Access denied</html>", "SERVER_BLOCKED"),
            (404, {"error": {"message": "The model `nope` does not exist or you do not have access to it.",
                             "type": "invalid_request_error", "code": "model_not_found"}}, "NOT_FOUND"),
            (400, {"error": {"message": "The model `llama3-70b-8192` has been decommissioned and is no longer "
                                        "supported.", "type": "invalid_request_error",
                             "code": "model_decommissioned"}}, "NOT_FOUND"),
            (429, {"error": {"message": "Rate limit reached ... https://console.groq.com/settings/billing",
                             "type": "tokens", "code": "rate_limit_exceeded"}}, "RATE_LIMITED"),
            (429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}, "QUOTA"),
            (500, {"error": {"message": "internal"}}, "UPSTREAM"),
            (503, "Service Unavailable", "UPSTREAM"),
            (400, {"error": {"message": "Failed to generate JSON", "code": "json_validate_failed"}}, "FORMAT"),
            (302, "", "ENDPOINT"),
        ]
        for status, body, reason in cases:
            with self.subTest(status=status, reason=reason):
                make = (lambda r, s=status, b=body: httpx.Response(s, json=b)) if isinstance(body, dict) else \
                    (lambda r, s=status, b=body: httpx.Response(s, text=b, headers={"location": "https://evil"}))
                failure = self.failure(make)
                self.assertEqual((failure.reason, failure.http_status, failure.route), (reason, status, "DIRECT"))
                self.assertIn(f"route=DIRECT result={reason} http_status={status}", self.logs[-1])

    def test_retry_after_is_reported_for_rate_limits(self):
        failure = self.failure(lambda r: httpx.Response(
            429, headers={"retry-after": "12"}, json={"error": {"code": "rate_limit_exceeded"}}))
        self.assertEqual((failure.reason, failure.retry_after), ("RATE_LIMITED", 12))
        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
        for raw, expected in (("0", 0), ("1.2", 2), ("999999", 3600), ("-4", 0), ("soon", None), ("nan", None),
                              ("inf", None), ("-inf", None), ("1e309", None)):
            self.assertEqual(_retry_after(httpx.Response(429, headers={"retry-after": raw})), expected, raw)
        self.assertIn(_retry_after(httpx.Response(429, headers={"retry-after": when})), range(85, 92))
        hostile = self.failure(lambda r: httpx.Response(429, headers={"retry-after": "1e309"},
                                                        json={"error": {"code": "rate_limit_exceeded"}}))
        self.assertEqual((hostile.reason, hostile.retry_after), ("RATE_LIMITED", None))

    def test_network_failures(self):
        for error, reason in ((httpx.ConnectTimeout("connect timed out to api.groq.com/openai"), "NETWORK"),
                              (httpx.ReadTimeout("read timed out"), "TIMEOUT"),
                              (httpx.ConnectError("[Errno -2] Name or service not known"), "NETWORK"),
                              (httpx.RemoteProtocolError(f"peer closed {GROQ} Bearer {KEY}"), "NETWORK")):
            with self.subTest(error=type(error).__name__):
                failure = self.failure(lambda r, e=error: e)
                self.assertEqual((failure.reason, failure.http_status, failure.route), (reason, None, "DIRECT"))
                self.assertIsNone(failure.__cause__)

    def test_malformed_and_incomplete_provider_output(self):
        cases = [
            (lambda r: httpx.Response(200, json={"choices": []}), "MALFORMED"),
            (lambda r: httpx.Response(200, json={"id": "x"}), "MALFORMED"),
            (lambda r: httpx.Response(200, text="<html>gateway</html>"), "MALFORMED"),
            (lambda r: httpx.Response(200, json=_ok(None)), "MALFORMED"),
            (lambda r: httpx.Response(200, json=_ok("not json at all")), "FORMAT"),
            (lambda r: httpx.Response(200, json=_ok('{"message":"ok"}')), "FORMAT"),
            (lambda r: httpx.Response(200, json=_ok('{"message":"ok","actions":"CREATE_TASK"}')), "FORMAT"),
            # max_tokens hit mid-answer: truncated JSON is a format failure, never a proposal.
            (lambda r: httpx.Response(200, json={"choices": [{"message": {"content": TYPED[:40]},
                                                               "finish_reason": "length"}]}), "FORMAT"),
        ]
        for handler, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.failure(handler).reason, reason)

    def test_standby_failover_only_for_credential_specific_reasons(self):
        for status, body, expected_calls in (
            (401, {"error": {"code": "invalid_api_key"}}, 2),
            (402, {"error": {"code": "insufficient_quota"}}, 2),
            (429, {"error": {"code": "rate_limit_exceeded", "message": "…/settings/billing"}}, 1),
            (403, {"error": {"message": "Forbidden"}}, 1),
            (503, {"error": {"message": "over capacity"}}, 1),
            (404, {"error": {"code": "model_not_found", "message": "model"}}, 1),
        ):
            with self.subTest(status=status):
                seen: list[str] = []

                def handler(request, status=status, body=body):
                    key = request.headers["authorization"]
                    seen.append(key)
                    return httpx.Response(status, json=body) if key.endswith("primary") else \
                        httpx.Response(200, json=_ok())

                pool = PlatformProviderPool([
                    build_provider("openai-compatible", api_key=KEY + "primary", model="m", base_url=GROQ),
                    build_provider("openai-compatible", api_key=KEY + "standby", model="m", base_url=GROQ),
                ])
                self.run_with(handler, pool)
                self.assertEqual(len(seen), expected_calls)

    def smoke(self, handler, provider=None) -> dict:
        transport = _Transport(handler)
        real_client = httpx.Client

        def post(url, **kwargs):
            kwargs.pop("trust_env")
            with real_client(transport=transport, trust_env=False, follow_redirects=kwargs.pop("follow_redirects"),
                             timeout=kwargs.pop("timeout")) as client:
                return client.post(url, **kwargs)

        with patch("student_execution_os.agent.providers.httpx.post", side_effect=post):
            report = platform_llm_smoke(provider or build_provider(
                "openai-compatible", api_key=KEY, model="openai/gpt-oss-20b", base_url=GROQ))
        self.assert_clean(repr(report))
        self.assertNotIn("api.groq.com", repr(report))
        return report

    def test_operator_smoke_reports_only_classifications(self):
        ok = self.smoke(lambda r: httpx.Response(200, json=_ok()))
        self.assertEqual({k: ok[k] for k in ("result", "route", "provider", "model", "total_tokens")},
                         {"result": "OK", "route": "DIRECT", "provider": "openai-compatible",
                          "model": "openai/gpt-oss-20b", "total_tokens": 18})
        blocked = self.smoke(lambda r: httpx.Response(403, json={"error": {"message": "Forbidden"}}))
        self.assertEqual((blocked["result"], blocked["http_status"], blocked["route"]),
                         ("SERVER_BLOCKED", 403, "DIRECT"))
        # A 2xx that is not a typed action is a failure; the local parser is never consulted.
        untyped = self.smoke(lambda r: httpx.Response(200, json=_ok('{"message":"hi","actions":[]}')))
        self.assertEqual(untyped["result"], "FORMAT")

    def test_operator_smoke_names_the_credential_that_answered(self):
        def handler(request):
            return httpx.Response(401, json={"error": {"code": "invalid_api_key"}}) \
                if request.headers["authorization"].endswith("primary") else httpx.Response(200, json=_ok())

        pool = PlatformProviderPool([
            build_provider("openai-compatible", api_key=KEY + "primary", model="m", base_url=GROQ),
            build_provider("openai-compatible", api_key=KEY + "standby", model="m", base_url=GROQ),
        ])
        report = self.smoke(handler, pool)
        self.assertEqual((report["result"], report["credential"], report["route"]), ("OK", "standby", "DIRECT"))
        with patch.dict(os.environ, {"SEOS_PLATFORM_LLM_PROVIDER": "openai-compatible"}, clear=True):
            self.assertEqual(platform_llm_smoke()["result"], "NOT_CONFIGURED")


class EgressRouteTests(unittest.TestCase):
    """Real socket layer: which address a request actually connects to."""

    def connect_spy(self):
        connected: list[tuple[str, int]] = []

        def create_connection(address, *args, **kwargs):
            connected.append(address)
            raise OSError("stop")

        return connected, create_connection

    def test_platform_direct_route_ignores_ambient_proxy_variables(self):
        connected, spy = self.connect_spy()
        provider = build_provider("openai-compatible", api_key=KEY, model="m", base_url=GROQ)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
                                     "https_proxy": "http://127.0.0.1:9"}, clear=True), \
                patch("socket.create_connection", side_effect=spy):
            with self.assertRaises(ProviderUnavailable) as caught:
                provider.interpret("x", {})
        self.assertEqual((caught.exception.reason, caught.exception.route), ("NETWORK", "DIRECT"))
        self.assertEqual(connected, [("api.groq.com", 443)])

    def test_user_supplied_address_is_pinned_against_dns_rebinding(self):
        connected, spy = self.connect_spy()
        # Validation sees a public address; every later lookup would rebind to loopback.
        answers = [_addr("93.184.216.34")]
        lookups: list[str] = []

        def resolver(host, *args, **kwargs):
            lookups.append(host)
            return answers.pop(0) if answers else _addr("127.0.0.1")

        provider = build_provider("openai-compatible", api_key=KEY, model="m",
                                  base_url="https://llm.example/v1", user_supplied=True)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9"}, clear=True), \
                patch("socket.getaddrinfo", side_effect=resolver), \
                patch("socket.create_connection", side_effect=spy):
            with self.assertRaises(ProviderUnavailable) as caught:
                provider.interpret("x", {})
        self.assertEqual((caught.exception.reason, caught.exception.route), ("NETWORK", "DIRECT_PINNED"))
        self.assertEqual(connected, [("93.184.216.34", 443)])  # the literal, never the name
        self.assertEqual(lookups, ["llm.example"])  # resolved exactly once, for validation

    def test_embedded_private_ipv4_forms_are_refused_for_user_addresses(self):
        for ip in ("64:ff9b::a00:1", "::127.0.0.1", "::ffff:10.0.0.1", "100.64.0.1"):
            with self.subTest(ip=ip), patch.dict(os.environ, {}, clear=True), \
                    patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr(ip)):
                with self.assertRaises(ProviderUnavailable) as caught:
                    assert_public_base_url("https://llm.example/v1")
                self.assertEqual(caught.exception.reason, "BLOCKED_URL")
        with patch.dict(os.environ, {}, clear=True), \
                patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr("93.184.216.34")):
            self.assertEqual(assert_public_base_url("https://llm.example/v1"),
                             (ipaddress.ip_address("93.184.216.34"),))

    def test_relay_route_is_logged_without_secrets_and_relay_errors_are_not_key_verdicts(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        token_file = Path(directory) / "relay.token"
        token_file.write_text(RELAY_TOKEN, encoding="utf-8")
        logs: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: logs.append(record.getMessage())  # type: ignore[method-assign]
        logger = logging.getLogger("student_execution_os.llm")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        env = {"SEOS_LLM_EGRESS_RELAY_URL": "https://relay.example",
               "SEOS_LLM_EGRESS_RELAY_TOKEN_FILE": str(token_file),
               "SEOS_LLM_EGRESS_RELAY_HOSTS": "api.groq.com"}
        cases = [
            (httpx.Response(200, json=_ok()), None),
            (httpx.Response(401, headers={"X-SEOS-Relay-Error": "unauthorized"},
                            json={"error": {"code": "unauthorized"}}), "REQUEST"),
            (httpx.Response(500, headers={"X-SEOS-Relay-Error": "relay_misconfigured"}), "REQUEST"),
            (httpx.Response(502, headers={"X-SEOS-Relay-Error": "provider_unreachable"}), "NETWORK"),
            (httpx.Response(401, json={"error": {"code": "invalid_api_key"}}), "AUTH"),  # Groq's own verdict
        ]
        for response, reason in cases:
            with self.subTest(reason=reason):
                transport = _Transport(lambda request, r=response: r)
                real_client = httpx.Client
                with patch.dict(os.environ, env, clear=True), \
                        patch("student_execution_os.agent.providers.socket.getaddrinfo",
                              return_value=_addr("104.21.1.1")), \
                        patch("student_execution_os.agent.providers.httpx.Client",
                              side_effect=lambda **kw: real_client(transport=transport, **kw)):
                    provider = build_provider("openai-compatible", api_key=KEY, model="m", base_url=GROQ)
                    try:
                        provider.interpret(PROMPT, {})
                        outcome = None
                    except ProviderUnavailable as exc:
                        outcome = exc.reason
                        self.assertEqual(exc.route, "RELAY")
                        for secret in (KEY, RELAY_TOKEN, PROMPT):
                            self.assertNotIn(secret, repr(exc))
                self.assertEqual(outcome, reason)
                request = transport.requests[-1]
                self.assertEqual(str(request.url), "https://relay.example/openai/v1/chat/completions")
                self.assertEqual(request.headers["x-seos-relay-token"], RELAY_TOKEN)
                self.assertIn("route=RELAY", logs[-1])
        for line in logs:
            for secret in (KEY, RELAY_TOKEN, PROMPT, "relay.example"):
                self.assertNotIn(secret, line)


if __name__ == "__main__":
    unittest.main()
