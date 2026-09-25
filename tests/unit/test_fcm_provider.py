"""FCM HTTP v1 provider: service-account JWT, token exchange, sends and verification.

Google's endpoints are replaced by an httpx MockTransport; the RSA key is generated
per test run, so the signature check is real.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from student_execution_os.reminders.push import FCM_SCOPE, FcmV1Provider, provider_from_environment


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGoogle:
    def __init__(self, public_key) -> None:
        self.public_key = public_key
        self.token_requests = 0
        self.sends: list[dict] = []
        self.send_status: dict[str, tuple[int, dict]] = {}
        self.token_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            assertion = form["assertion"][0]
            header, claims, signature = assertion.split(".")
            self.public_key.verify(_b64decode(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
            self.claims = json.loads(_b64decode(claims))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"ya29.access-{self.token_requests}", "expires_in": 3600})
        assert request.url.path == "/v1/projects/demo-project/messages:send", request.url.path
        assert request.headers["Authorization"].startswith("Bearer ya29.access-")
        body = json.loads(request.content)
        self.sends.append(body)
        token = body["message"]["token"]
        status, payload = self.send_status.get(token, (200, {"name": f"projects/demo-project/messages/{len(self.sends)}"}))
        if body.get("validate_only") and status == 200:
            payload = {"name": "projects/demo-project/messages/fake_message_id"}
        return httpx.Response(status, json=payload)


class FcmProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode()
        self.account = {"type": "service_account", "project_id": "demo-project", "private_key": self.pem,
                        "client_email": "fcm@demo-project.iam.gserviceaccount.com",
                        "token_uri": "https://oauth2.example/token"}
        self.google = FakeGoogle(key.public_key())
        self.http = httpx.Client(transport=httpx.MockTransport(self.google))
        self.provider = FcmV1Provider(self.account, http=self.http, fcm_base_url="https://fcm.example")

    def test_signed_assertion_is_exchanged_once_and_cached(self):
        first = self.provider.send("device-1", {"title": "T", "body": "B", "collapse_key": "task-1"}, data_only=True)
        second = self.provider.send("device-1", {"title": "T", "body": "B", "collapse_key": "task-1"})
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(self.google.token_requests, 1)
        self.assertEqual(self.google.claims["scope"], FCM_SCOPE)
        self.assertEqual(self.google.claims["iss"], self.account["client_email"])
        self.assertEqual(self.google.claims["aud"], "https://oauth2.example/token")
        data_only, rendered = self.google.sends
        self.assertNotIn("notification", data_only["message"])
        self.assertEqual(data_only["message"]["data"]["render"], "native")
        self.assertEqual(rendered["message"]["notification"], {"title": "T", "body": "B"})
        self.assertEqual(rendered["message"]["android"]["collapse_key"], "task-1")

    def test_error_mapping(self):
        self.google.send_status = {
            "gone": (404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}),
            "bad": (400, {"error": {"status": "INVALID_ARGUMENT"}}),
            "busy": (503, {"error": {"status": "UNAVAILABLE"}}),
            "expired": (401, {"error": {"status": "UNAUTHENTICATED"}}),
        }
        gone = self.provider.send("gone", {"title": "x"})
        self.assertTrue(gone.token_invalid and not gone.retryable)
        self.assertTrue(self.provider.send("bad", {"title": "x"}).token_invalid)
        self.assertTrue(self.provider.send("busy", {"title": "x"}).retryable)
        expired = self.provider.send("expired", {"title": "x"})
        self.assertTrue(expired.retryable)
        self.provider.send("device", {"title": "x"})
        self.assertEqual(self.google.token_requests, 2)  # a 401 drops the cached access token

    def test_oauth_failure_is_retryable_and_reported_without_secrets(self):
        self.google.token_status = 400
        result = self.provider.send("device", {"title": "x"})
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        report = self.provider.verify()
        self.assertEqual(report["oauth"], "FAILED")
        self.assertFalse(report["ok"])
        self.assertNotIn("PRIVATE KEY", json.dumps(report))

    def test_verify_checks_permission_and_device_tokens_without_delivering(self):
        self.google.send_status = {
            "seos-placeholder-token": (400, {"error": {"status": "INVALID_ARGUMENT",
                                                      "details": [{"errorCode": "INVALID_ARGUMENT"}]}}),
            "stale-device": (404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}),
        }
        report = self.provider.verify(["live-device", "stale-device"])
        self.assertTrue(report["ok"])
        self.assertEqual(report["oauth"], "OK")
        self.assertEqual(report["project_permission"], "OK")
        self.assertEqual(report["devices"], ["VALID", "HTTP_404:NOT_FOUND,UNREGISTERED"])
        self.assertTrue(all(send["validate_only"] for send in self.google.sends))
        serialized = json.dumps(report)
        self.assertNotIn("ya29", serialized)
        self.assertNotIn("PRIVATE KEY", serialized)
        self.google.send_status["seos-placeholder-token"] = (403, {"error": {"status": "PERMISSION_DENIED"}})
        denied = self.provider.verify()
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["project_permission"], "HTTP_403:PERMISSION_DENIED")

    def test_environment_file_and_bad_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fcm.json"
            path.write_text(json.dumps(self.account), encoding="utf-8")
            with patch.dict(os.environ, {"SEOS_FCM_SERVICE_ACCOUNT_JSON": "", "SEOS_FCM_SERVICE_ACCOUNT_FILE": str(path)}):
                provider = provider_from_environment()
                self.assertTrue(provider.configured)
                self.assertEqual(provider.project_id, "demo-project")
            log = io.StringIO()
            handler = logging.StreamHandler(log)
            logging.getLogger("student_execution_os.push").addHandler(handler)
            try:
                broken = dict(self.account, client_email="")
                with patch.dict(os.environ, {"SEOS_FCM_SERVICE_ACCOUNT_JSON": json.dumps(broken)}):
                    self.assertFalse(provider_from_environment().configured)
                with patch.dict(os.environ, {"SEOS_FCM_SERVICE_ACCOUNT_JSON": "",
                                             "SEOS_FCM_SERVICE_ACCOUNT_FILE": str(Path(tmp) / "missing.json")}):
                    self.assertFalse(provider_from_environment().configured)
            finally:
                logging.getLogger("student_execution_os.push").removeHandler(handler)
            self.assertNotIn("PRIVATE KEY", log.getvalue())
            self.assertNotIn(self.pem[40:80], log.getvalue())


if __name__ == "__main__":
    unittest.main()
