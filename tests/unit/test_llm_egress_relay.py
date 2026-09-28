"""The operator's application relay (e.g. the Cloudflare Groq relay) for LLM egress."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from student_execution_os.agent import providers
from student_execution_os.agent.providers import ProviderUnavailable, build_provider
from student_execution_os.netguard import PinnedTransport

RELAY = "https://seos-groq-relay.example.workers.dev"
RELAY_TOKEN = "relay-secret-" + "r" * 40
GROQ = "https://api.groq.com/openai/v1"
GROQ_KEY = "gsk-test-123456789"
OK = {"choices": [{"message": {"content":
      '{"message":"Preview","actions":[{"command":"CREATE_TASK","payload":{"title":"Essay"},"confidence":0.9,'
      '"unresolved_fields":[],"expected_version":null,"requires_confirmation":false}]}'}}]}


def _addr(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


def _response(payload, status=200, headers=None):
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    result.headers = _Headers(headers or {})
    return result


class _Headers(dict):
    """Case-insensitive like httpx.Headers, enough for .get()."""

    def __init__(self, values):
        super().__init__({k.lower(): v for k, v in values.items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class LlmEgressRelayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token_file = Path(directory.name) / "llm-egress-relay.token"
        self.token_file.write_text(RELAY_TOKEN + "\n", encoding="utf-8")
        client_patch = patch("student_execution_os.agent.providers.httpx.Client")
        post_patch = patch("student_execution_os.agent.providers.httpx.post")
        dns_patch = patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr("104.21.1.1"))
        self.client_type = client_patch.start()
        self.direct_post = post_patch.start()
        self.getaddrinfo = dns_patch.start()
        for started in (client_patch, post_patch, dns_patch):
            self.addCleanup(started.stop)
        self.relay_post = self.client_type.return_value.__enter__.return_value.post
        self.relay_post.return_value = _response(OK)
        self.direct_post.return_value = _response(OK)

    def env(self, **overrides):
        values = {
            "SEOS_LLM_EGRESS_RELAY_URL": RELAY,
            "SEOS_LLM_EGRESS_RELAY_TOKEN_FILE": str(self.token_file),
            "SEOS_LLM_EGRESS_RELAY_HOSTS": "api.groq.com",
        }
        values.update(overrides)
        return patch.dict(os.environ, {k: v for k, v in values.items() if v is not None}, clear=True)

    def groq(self, base=GROQ):
        return build_provider("openai-compatible", api_key=GROQ_KEY, model="openai/gpt-oss-20b",
                              base_url=base, user_supplied=True)

    def failure(self, provider=None) -> ProviderUnavailable:
        with self.assertRaises(ProviderUnavailable) as caught:
            (provider or self.groq()).check()
        return caught.exception

    def assert_direct_pinned(self, url: str) -> None:
        """A user-supplied address goes direct, pinned to its validated DNS answer."""
        self.direct_post.assert_not_called()
        self.client_type.assert_called_once()
        kwargs = self.client_type.call_args.kwargs
        self.assertNotIn("proxy", kwargs)
        self.assertFalse(kwargs["trust_env"])
        self.assertFalse(kwargs["follow_redirects"])
        self.assertIsInstance(kwargs["transport"], PinnedTransport)
        self.assertEqual(self.relay_post.call_args.args[0], url)
        self.assertNotIn("X-SEOS-Relay-Token", self.relay_post.call_args.kwargs["headers"])

    # -- routing --------------------------------------------------------------

    def test_no_relay_configuration_goes_direct(self):
        with patch.dict(os.environ, {}, clear=True):
            self.groq().interpret("Essay", {})
        self.assert_direct_pinned(GROQ + "/chat/completions")

    def test_exact_groq_host_is_sent_to_the_relay_with_both_credentials(self):
        with self.env():
            self.assertEqual(self.groq().interpret("Essay", {})["actions"][0]["command"], "CREATE_TASK")
        self.direct_post.assert_not_called()
        self.client_type.assert_called_once_with(trust_env=False, timeout=30.0, follow_redirects=False)
        self.assertNotIn("verify", self.client_type.call_args.kwargs)
        self.assertNotIn("proxy", self.client_type.call_args.kwargs)
        self.assertEqual(self.relay_post.call_args.args[0], RELAY + "/openai/v1/chat/completions")
        headers = self.relay_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], f"Bearer {GROQ_KEY}")
        self.assertEqual(headers["X-SEOS-Relay-Token"], RELAY_TOKEN)
        body = self.relay_post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "openai/gpt-oss-20b")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("Essay", body["messages"][1]["content"])

    def test_host_normalisation_is_exact(self):
        with self.env(SEOS_LLM_EGRESS_RELAY_HOSTS=" API.Groq.com. "):
            self.groq("https://API.GROQ.COM./openai/v1").check()
        self.client_type.assert_called_once()
        self.direct_post.assert_not_called()

    def test_other_hosts_go_direct(self):
        for base in ("https://llm.example/v1", "https://api.groq.com.attacker.example/openai/v1",
                     "https://evil.api.groq.com/openai/v1", "https://groq.com/openai/v1",
                     "https://www.api.groq.com/openai/v1", "https://api.groq.com:8443/openai/v1"):
            self.direct_post.reset_mock()
            self.client_type.reset_mock()
            with self.env():
                self.groq(base).check()
            self.assert_direct_pinned(base + "/chat/completions")

    def test_ssrf_check_on_the_user_address_still_runs_before_any_transport(self):
        self.getaddrinfo.return_value = _addr("127.0.0.1")
        with self.env():
            failure = self.failure()
        self.assertEqual(failure.reason, "BLOCKED_URL")
        self.getaddrinfo.assert_called_once()
        self.assertEqual(self.getaddrinfo.call_args.args[0], "api.groq.com")
        self.client_type.assert_not_called()
        self.direct_post.assert_not_called()

    # -- configuration ----------------------------------------------------------

    def test_missing_host_allowlist_is_a_request_error(self):
        for hosts in (None, "", " , "):
            with self.env(SEOS_LLM_EGRESS_RELAY_HOSTS=hosts):
                self.assertEqual(self.failure().reason, "REQUEST")
        self.client_type.assert_not_called()
        self.direct_post.assert_not_called()

    def test_missing_unreadable_or_empty_token_file_is_a_request_error(self):
        empty = self.token_file.with_name("empty.token")
        empty.write_text("\n", encoding="utf-8")
        for file_name in (None, "", str(self.token_file.with_name("absent.token")), str(empty)):
            with self.env(SEOS_LLM_EGRESS_RELAY_TOKEN_FILE=file_name):
                failure = self.failure()
            self.assertEqual(failure.reason, "REQUEST", file_name)
            self.assertNotIn(RELAY_TOKEN, str(failure))
        self.client_type.assert_not_called()
        self.direct_post.assert_not_called()

    def test_relay_url_must_be_a_bare_public_https_origin(self):
        for bad in ("http://seos-groq-relay.example.workers.dev", "https://user:pw@relay.example",
                    "https://relay.example/?url=https://evil.example", "https://relay.example/prefix",
                    "https://relay.example/#x", "ftp://relay.example"):
            with self.env(SEOS_LLM_EGRESS_RELAY_URL=bad):
                self.assertEqual(self.failure().reason, "REQUEST", bad)
        for ip in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
            self.getaddrinfo.side_effect = lambda host, *args, ip=ip, **kwargs: _addr("104.18.2.2" if host == "api.groq.com" else ip)
            with self.env(SEOS_LLM_EGRESS_RELAY_URL="https://relay.example"):
                self.assertEqual(self.failure().reason, "REQUEST", ip)
        self.client_type.assert_not_called()
        self.direct_post.assert_not_called()

    def test_proxy_and_relay_for_the_same_request_is_refused(self):
        for proxy in ({"SEOS_LLM_EGRESS_PROXY": "http://llm-egress-proxy:8118", "SEOS_LLM_EGRESS_PROXY_HOSTS": "api.groq.com"},):
            with self.env(**proxy):
                failure = self.failure()
            self.assertEqual(failure.reason, "REQUEST")
            self.assertIn("only one LLM egress mechanism", str(failure))
        self.client_type.assert_not_called()
        self.direct_post.assert_not_called()

    def test_proxy_for_another_host_does_not_conflict(self):
        with self.env(SEOS_LLM_EGRESS_PROXY="http://proxy.example:3128", SEOS_LLM_EGRESS_PROXY_HOSTS="api.openai.com"):
            self.groq().check()
        self.client_type.assert_called_once_with(trust_env=False, timeout=30.0, follow_redirects=False)

    def test_secrets_are_not_in_repr_or_errors(self):
        with self.env():
            relay = providers._llm_egress_relay(GROQ + "/chat/completions")
        self.assertNotIn(RELAY_TOKEN, repr(relay))
        self.relay_post.return_value = _response({"error": {"message": f"echo {RELAY_TOKEN} {GROQ_KEY}"}}, 401,
                                                 {"X-SEOS-Relay-Error": "unauthorized"})
        with self.env():
            failure = self.failure()
        for secret in (RELAY_TOKEN, GROQ_KEY):
            self.assertNotIn(secret, str(failure))
            self.assertNotIn(secret, repr(failure))
        self.assertNotIn(GROQ_KEY, repr(self.groq()))

    # -- error classification --------------------------------------------------

    def test_relay_generated_errors_are_not_the_users_key(self):
        cases = (
            (401, "unauthorized", "REQUEST"),
            (400, "invalid_request", "REQUEST"),
            (415, "invalid_request", "REQUEST"),
            (413, "invalid_request", "REQUEST"),
            (404, "not_found", "REQUEST"),
            (500, "relay_misconfigured", "REQUEST"),
            (502, "provider_unreachable", "NETWORK"),
            (502, "upstream_redirect", "UPSTREAM"),
        )
        for status, code, reason in cases:
            self.relay_post.return_value = _response({"error": {"type": "seos_relay_error", "code": code}}, status,
                                                     {"X-SEOS-Relay-Error": code})
            with self.env():
                failure = self.failure()
            self.assertEqual((failure.reason, failure.http_status), (reason, status), code)
            self.assertIn("relay", str(failure))

    def test_provider_errors_through_the_relay_keep_their_classification(self):
        cases = (
            (401, {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}, "AUTH"),
            (403, {"error": {"code": "unsupported_country_region_territory", "type": "request_forbidden",
                             "message": "Country, region, or territory not supported"}}, "SERVER_BLOCKED"),
            (403, {"error": {"message": "Forbidden"}}, "SERVER_BLOCKED"),
            (403, {"error": {"type": "permissions_error", "code": "model_permission_blocked_project",
                             "message": "The model openai/gpt-oss-20b is blocked at the project level."}}, "NOT_FOUND"),
            (429, {"error": {"message": "Rate limit reached"}}, "RATE_LIMITED"),
            (429, {"error": {"message": "insufficient_quota"}}, "QUOTA"),
            (503, {"error": {"message": "over capacity"}}, "UPSTREAM"),
        )
        for status, body, reason in cases:
            self.relay_post.return_value = _response(body, status)
            with self.env():
                failure = self.failure()
            self.assertEqual((failure.reason, failure.http_status), (reason, status), body)

    def test_relay_error_header_is_ignored_on_direct_requests(self):
        self.relay_post.return_value = _response({"error": {"message": "Invalid API Key"}}, 401,
                                                  {"X-SEOS-Relay-Error": "unauthorized"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.failure().reason, "AUTH")


class RelaySourceInvariantTests(unittest.TestCase):
    def test_transport_security_is_explicit_in_source(self):
        source = (Path(providers.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("verify=False", source)
        self.assertIn("httpx.Client(trust_env=False, timeout=timeout, follow_redirects=False)", source)


if __name__ == "__main__":
    unittest.main()
