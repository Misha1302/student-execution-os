from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import Mock, patch

from student_execution_os.agent.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderUnavailable,
    assert_public_base_url,
    build_provider,
    platform_provider_from_environment,
)
from student_execution_os.domain.errors import ValidationError


def _addr(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


class AssistantProviderTests(unittest.TestCase):
    def response(self, payload, status=200):
        result = Mock()
        result.status_code = status
        result.json.return_value = payload
        return result

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_openai_compatible_returns_structured_preview(self, post):
        post.return_value = self.response({"choices":[{"message":{"content":
            '{"message":"Preview","actions":[{"command":"CREATE_TASK","payload":{"title":"Essay"},"confidence":0.9,"unresolved_fields":["estimated_total_effort_minutes"],"expected_version":null,"requires_confirmation":false}]}'}}]})
        result = OpenAICompatibleProvider("server-secret", "configured-model", "https://llm.example/v1",
                                          name="openai-compatible").interpret("task: Essay", {})
        self.assertEqual(result["actions"][0]["command"], "CREATE_TASK")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer server-secret")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "configured-model")
        self.assertEqual(post.call_args.kwargs["json"]["temperature"], 0)
        self.assertFalse(post.call_args.kwargs["follow_redirects"])

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_official_openai_omits_temperature_for_reasoning_models(self, post):
        post.return_value = self.response({"choices": [{"message": {"content": '{"message":"","actions":[]}'}}]})
        build_provider("openai", api_key="sk-test-123456789", model="gpt-5-mini").interpret("hi", {})
        self.assertNotIn("temperature", post.call_args.kwargs["json"])
        self.assertTrue(post.call_args.args[0].startswith("https://api.openai.com/v1/"))

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_anthropic_adapter_uses_configured_model(self, post):
        post.return_value = self.response({"content":[{"text":'{"message":"Nothing to change","actions":[]}'}]})
        result = AnthropicProvider("server-secret", "claude-configured").interpret("hello", {})
        self.assertEqual(result["actions"], [])
        self.assertEqual(post.call_args.kwargs["json"]["model"], "claude-configured")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "server-secret")

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_http_errors_become_reason_codes_without_provider_body(self, post):
        for status, reason in ((401, "AUTH"), (403, "AUTH"), (404, "NOT_FOUND"), (429, "RATE_LIMITED"),
                               (400, "REJECTED"), (503, "UPSTREAM")):
            post.return_value = self.response({"error": {"message": "Incorrect API key provided: sk-abc...wxyz"}}, status)
            with self.assertRaises(ProviderUnavailable) as caught:
                AnthropicProvider("sk-abcdefghijwxyz", "m").check()
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("wxyz", str(caught.exception))

    def test_provider_repr_never_shows_the_key(self):
        for provider in (build_provider("openai", api_key="sk-supersecret-1234", model="m"),
                         build_provider("anthropic", api_key="sk-supersecret-1234", model="m")):
            self.assertNotIn("supersecret", repr(provider))

    def test_user_supplied_base_url_must_be_public_https(self):
        with patch.dict(os.environ, {}, clear=True):
            for bad in ("http://llm.example/v1", "https://user:pw@llm.example/v1", "ftp://llm.example"):
                with self.assertRaises(ProviderUnavailable):
                    assert_public_base_url(bad)
            for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.2", "169.254.169.254", "::1", "172.17.0.1", "100.64.0.1"):
                with patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr(ip)):
                    with self.assertRaises(ProviderUnavailable) as caught:
                        assert_public_base_url("https://llm.example/v1")
                    self.assertEqual(caught.exception.reason, "BLOCKED_URL", ip)
            with patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr("93.184.216.34")):
                assert_public_base_url("https://llm.example/v1")

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_user_supplied_address_is_rechecked_before_each_request(self, post):
        provider = build_provider("openai-compatible", api_key="k" * 20, model="m",
                                  base_url="https://llm.example/v1", user_supplied=True)
        with patch.dict(os.environ, {}, clear=True), \
             patch("student_execution_os.agent.providers.socket.getaddrinfo", return_value=_addr("127.0.0.1")):
            with self.assertRaises(ProviderUnavailable):
                provider.check()
        post.assert_not_called()

    def test_platform_credentials_come_only_from_platform_variables(self):
        with patch.dict(os.environ, {"SEOS_LLM_PROVIDER": "openai", "SEOS_LLM_API_KEY": "x" * 20,
                                     "SEOS_LLM_MODEL": "m"}, clear=True):
            self.assertIsNone(platform_provider_from_environment())
        with patch.dict(os.environ, {"SEOS_PLATFORM_LLM_PROVIDER": "openai-compatible", "SEOS_PLATFORM_LLM_API_KEY": "x",
                                     "SEOS_PLATFORM_LLM_MODEL": "m", "SEOS_PLATFORM_LLM_BASE_URL": "https://llm.example/v1"},
                        clear=True):
            provider = platform_provider_from_environment()
            self.assertEqual(provider.name, "openai-compatible")
            self.assertEqual(provider.model, "m")
            self.assertFalse(provider.public_only)
        with self.assertRaises(ValidationError):
            build_provider("openai-compatible", api_key="x", model="m")
        with self.assertRaises(ValidationError):
            build_provider("gemini", api_key="x", model="m")


if __name__ == "__main__": unittest.main()
