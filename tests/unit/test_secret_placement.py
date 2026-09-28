"""G6 Compose secret-placement evidence is safe and matches supported proxy modes."""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("check_secret_placement",
                                              Path("tools/check_secret_placement.py"))
placement = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(placement)


class SecretPlacementTest(unittest.TestCase):
    def test_file_secrets_and_credential_free_tor_proxy_pass(self):
        config = {"services": {"api": {"environment": {
            "SEOS_CREDENTIAL_KEY_FILE": "/run/secrets/seos/credential.key",
            "SEOS_PLATFORM_LLM_API_KEY_FILES": "/run/secrets/seos/groq-1.key",
            "SEOS_PLATFORM_LLM_API_KEY": "",
            "SEOS_LLM_EGRESS_PROXY": "http://llm-egress-proxy:8118",
        }}, "worker": {"environment": {
            "SEOS_FCM_SERVICE_ACCOUNT_FILE": "/run/secrets/seos/fcm.json",
            "SEOS_FCM_SERVICE_ACCOUNT_JSON": "",
        }}}}
        result = placement.check(config)
        self.assertEqual(result["result"], "OK")
        self.assertEqual({item["name"] for item in result["file_placements"]},
                         {"SEOS_CREDENTIAL_KEY_FILE", "SEOS_PLATFORM_LLM_API_KEY_FILES",
                          "SEOS_FCM_SERVICE_ACCOUNT_FILE"})
        rendered = json.dumps(result)
        self.assertNotIn("llm-egress-proxy", rendered)

    def test_inline_secrets_and_proxy_userinfo_fail_without_echoing_values(self):
        platform_key = "platform-sensitive-value"
        fcm_json = "sensitive-service-account-json"
        proxy = "http://operator:sensitive-password@proxy.example:3128"
        config = {"services": {"api": {"environment": {
            "SEOS_PLATFORM_LLM_API_KEY": platform_key,
            "SEOS_LLM_EGRESS_PROXY": proxy,
            "SEOS_LLM_EGRESS_RELAY_TOKEN": "sensitive-relay-token",
        }}, "worker": {"environment": {"SEOS_FCM_SERVICE_ACCOUNT_JSON": fcm_json}}}}
        result = placement.check(config)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual({item["name"] for item in result["violations"]},
                         {"SEOS_PLATFORM_LLM_API_KEY", "SEOS_LLM_EGRESS_PROXY",
                          "SEOS_LLM_EGRESS_RELAY_TOKEN", "SEOS_FCM_SERVICE_ACCOUNT_JSON"})
        rendered = json.dumps(result)
        for secret in (platform_key, fcm_json, proxy, "sensitive-password", "sensitive-relay-token"):
            self.assertNotIn(secret, rendered)

    def test_bad_compose_shape_fails_closed(self):
        with self.assertRaises(ValueError):
            placement.check({})
        with self.assertRaises(ValueError):
            placement.check({"services": {"api": {"environment": []}}})


if __name__ == "__main__":
    unittest.main()
