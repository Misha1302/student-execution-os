"""The AI connection test is a real interpret() round trip, and every way it can fail
has its own reason (not "any HTTP 2xx is fine")."""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import httpx

from student_execution_os.agent.providers import ProviderUnavailable, build_provider

GOOD = json.dumps({"message": "ok", "actions": [{
    "command": "CREATE_TASK", "payload": {"title": "Проверка связи", "estimated_total_effort_minutes": 15},
    "confidence": 0.9, "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}]})


def openai_body(content):
    return {"choices": [{"message": {"content": content}}]}


def response(status, body=None, *, json_ok=True):
    r = Mock()
    r.status_code = status
    if json_ok:
        r.json.return_value = body
    else:
        r.json.side_effect = ValueError("not json")
    return r


class ConnectionCheckTest(unittest.TestCase):
    def check(self, kind, reply, **build):
        provider = build_provider(kind, api_key="sk-secret-000011112222", model="m", **build)
        with patch("student_execution_os.agent.providers.httpx.post") as post:
            if isinstance(reply, Exception):
                post.side_effect = reply
            else:
                post.return_value = reply
            try:
                provider.check()
            except ProviderUnavailable as exc:
                self.assertNotIn("000011112222", str(exc))
                return exc.reason, post
            return "OK", post

    def test_a_2xx_is_not_enough_the_answer_must_be_a_typed_action(self):
        cases = {
            "OK": response(200, openai_body(GOOD)),
            # 200 with prose instead of JSON: the model cannot do what capture needs.
            "FORMAT": response(200, openai_body("Sure! I'd be happy to help.")),
            # 200 with JSON but no typed create action.
            "FORMAT ": response(200, openai_body(json.dumps({"message": "hi", "actions": []}))),
            # 200 whose body is not the provider API at all.
            "MALFORMED": response(200, {"status": "ok"}),
            "MALFORMED ": response(200, None, json_ok=False),
        }
        for expected, reply in cases.items():
            with self.subTest(expected=expected):
                reason, post = self.check("openai", reply)
                self.assertEqual(reason, expected.strip())
                sent = post.call_args.kwargs["json"]
                # The probe is shaped exactly like interpret(): system prompt + JSON mode.
                self.assertEqual(sent["response_format"], {"type": "json_object"})
                self.assertIn("CREATE_TASK", sent["messages"][0]["content"])

    def test_http_failures_are_told_apart(self):
        cases = [
            (response(401, {"error": {"message": "Incorrect API key sk-...2222", "code": "invalid_api_key"}}), "AUTH"),
            (response(404, {"error": {"message": "The model `m` does not exist", "code": "model_not_found"}}), "NOT_FOUND"),
            (response(429, {"error": {"message": "Rate limit", "code": "rate_limit_exceeded"}}), "RATE_LIMITED"),
            (response(429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}), "QUOTA"),
            (response(400, {"error": {"message": "Invalid parameter", "param": "response_format"}}), "FORMAT"),
            (response(400, {"error": {"message": "something else"}}), "REJECTED"),
            (response(503, None, json_ok=False), "UPSTREAM"),
            (response(301, None, json_ok=False), "ENDPOINT"),
            (httpx.ConnectError("boom"), "NETWORK"),
            (httpx.ReadTimeout("slow"), "NETWORK"),
        ]
        for reply, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self.check("openai", reply)[0], expected)

    def test_wrong_address_of_a_compatible_provider_is_not_a_model_problem(self):
        with patch("student_execution_os.agent.providers.assert_public_base_url"):
            html_404 = response(404, None, json_ok=False)
            self.assertEqual(self.check("openai-compatible", html_404, base_url="https://api.example.com/v2")[0], "ENDPOINT")
            api_404 = response(404, {"error": {"message": "model not found"}})
            self.assertEqual(self.check("openai-compatible", api_404, base_url="https://api.example.com/v1")[0], "NOT_FOUND")

    def test_anthropic_probe_and_failures(self):
        ok = response(200, {"content": [{"type": "text", "text": GOOD}]})
        self.assertEqual(self.check("anthropic", ok)[0], "OK")
        self.assertEqual(self.check("anthropic", response(200, {"content": [{"type": "text", "text": "OK"}]}))[0], "FORMAT")
        self.assertEqual(self.check("anthropic", response(200, {"id": "x"}))[0], "MALFORMED")
        not_found = response(404, {"type": "error", "error": {"type": "not_found_error", "message": "model: m"}})
        self.assertEqual(self.check("anthropic", not_found)[0], "NOT_FOUND")
        credit = response(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                           "message": "Your credit balance is too low"}})
        self.assertEqual(self.check("anthropic", credit)[0], "QUOTA")
        self.assertEqual(self.check("anthropic", response(529, None, json_ok=False))[0], "UPSTREAM")


if __name__ == "__main__":
    unittest.main()
