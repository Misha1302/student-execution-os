from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from student_execution_os.agent.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    capabilities,
    provider_from_environment,
)


class AssistantProviderTests(unittest.TestCase):
    def response(self, payload):
        result = Mock()
        result.raise_for_status.return_value = None
        result.json.return_value = payload
        return result

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_openai_compatible_returns_structured_preview(self, post):
        post.return_value = self.response({"choices":[{"message":{"content":
            '{"message":"Preview","actions":[{"command":"CREATE_TASK","payload":{"title":"Essay"},"confidence":0.9,"unresolved_fields":["estimated_total_effort_minutes"],"expected_version":null,"requires_confirmation":false}]}'}}]})
        result = OpenAICompatibleProvider("server-secret", "configured-model", "https://llm.example/v1").interpret("task: Essay", {})
        self.assertEqual(result["actions"][0]["command"], "CREATE_TASK")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer server-secret")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "configured-model")

    @patch("student_execution_os.agent.providers.httpx.post")
    def test_anthropic_adapter_uses_configured_model(self, post):
        post.return_value = self.response({"content":[{"text":'{"message":"Nothing to change","actions":[]}'}]})
        result = AnthropicProvider("server-secret", "claude-configured").interpret("hello", {})
        self.assertEqual(result["actions"], [])
        self.assertEqual(post.call_args.kwargs["json"]["model"], "claude-configured")

    def test_environment_provider_selection_and_degraded_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(provider_from_environment())
            self.assertTrue(capabilities()["degraded_mode"])
        with patch.dict(os.environ, {"SEOS_LLM_PROVIDER":"openai-compatible", "SEOS_LLM_API_KEY":"x",
                                    "SEOS_LLM_MODEL":"m", "SEOS_LLM_BASE_URL":"https://llm.example/v1"}, clear=True):
            provider = provider_from_environment()
            self.assertEqual(provider.name, "openai-compatible")
            self.assertEqual(provider.model, "m")


if __name__ == "__main__": unittest.main()
