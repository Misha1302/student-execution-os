"""Per-account LLM credentials (BYOK) over the real HTTP API.

Covers the lifecycle (set → test → use → change → delete → account deletion), the
local-parser fallback, isolation between accounts, secret hygiene of every response,
log and storage surface, and the PLATFORM_MANAGED entitlement seam.
"""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from student_execution_os.agent.credentials import (
    TEST_LIMITER,
    CredentialCipher,
    CredentialUnreadable,
    LlmCredentialStore,
    key_hint,
)
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
KEY_A = "sk-alice-0123456789-AAAA"
KEY_B = "sk-ant-bob-9876543210-BBBB"
MASTER = CredentialCipher.generate_key()


def _proposal(title: str) -> str:
    return json.dumps({"message": "ok", "actions": [{
        "command": "CREATE_TASK", "payload": {"title": title, "estimated_total_effort_minutes": 45},
        "confidence": 0.9, "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}]})


class FakeLlm:
    """Stands in for every provider endpoint and records which key reached it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.status: dict[str, int] = {}
        self.error_body: dict | None = None  # what a non-2xx answer carries
        self.during_call = None  # runs while a request is "in flight"

    def __call__(self, url, *, headers, json, timeout, follow_redirects):
        key = headers.get("x-api-key") or headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append((url, key))
        self.bodies.append(json)
        if self.during_call is not None:
            self.during_call()
        response = Mock()
        response.status_code = self.status.get(key, 200)
        if response.status_code >= 300 and self.error_body is not None:
            response.json.return_value = self.error_body
        elif "/v1/messages" in url:
            response.json.return_value = {"content": [{"text": _proposal(f"anthropic:{key[-4:]}")}]}
        else:
            response.json.return_value = {"choices": [{"message": {"content": _proposal(f"openai:{key[-4:]}")}}]}
        return response


class LlmCredentialsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "llm.sqlite")
        self.env = patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": MASTER}, clear=False)
        self.env.start()
        for name in [n for n in os.environ if n.startswith(("SEOS_PLATFORM_LLM_", "SEOS_LLM_"))]:
            os.environ.pop(name)
        self.fake = FakeLlm()
        self.post = patch("student_execution_os.agent.providers.httpx.post", side_effect=self.fake)
        self.post.start()
        TEST_LIMITER._hits.clear()
        self.client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger().addHandler(self.handler)
        logging.getLogger().setLevel(logging.DEBUG)
        self.alice = self._register("alice")
        self.bob = self._register("bob")
        self.carol = self._register("carol")

    def tearDown(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.post.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _register(self, login: str) -> dict[str, str]:
        response = self.client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}", "account": body["user"]["account_id"], "login": login}

    def _h(self, who):
        return {"Authorization": who["Authorization"]}

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        for secret in (KEY_A, KEY_B, MASTER, "0123456789", "9876543210"):
            self.assertNotIn(secret, response.text)
        return response.json()

    def interpret(self, who, text="сдать эссе"):
        return self.ok(self.client.post("/api/v1/assistant/interpret", headers=self._h(who), json={"text": text}))

    def save(self, who, **payload):
        return self.client.put("/api/v1/settings/llm", headers=self._h(who), json=payload)

    # ---------------------------------------------------------------------------------

    def test_new_account_without_key_uses_local_parser(self):
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.carol)))
        self.assertEqual(settings["source"], "NONE")
        self.assertIsNone(settings["credential"])
        self.assertTrue(settings["storage_available"])
        self.assertEqual({p["id"] for p in settings["providers"]}, {"openai", "anthropic", "openai-compatible"})
        caps = self.ok(self.client.get("/api/v1/ask/capabilities", headers=self._h(self.carol)))
        self.assertFalse(caps["live_llm_provider"])
        self.assertEqual(caps["credential_source"], "NONE")
        preview = self.interpret(self.carol, "В пятницу к шести сдать лабораторную, займёт часа два")
        self.assertEqual(preview["provider"], "deterministic-local-v1")
        self.assertEqual(preview["actions"][0]["payload"]["estimated_total_effort_minutes"], 120)
        self.assertEqual(self.fake.calls, [])
        diag = self.ok(self.client.get("/api/v1/settings/diagnostics", headers=self._h(self.carol)))
        self.assertEqual(diag["external_capabilities"]["llm"], "NONE")

    def test_full_byok_lifecycle_and_isolation(self):
        saved = self.ok(self.save(self.alice, provider="openai", model="gpt-5-mini", api_key=KEY_A))
        self.assertEqual(saved["source"], "USER_BYOK")
        self.assertEqual(saved["credential"]["key_hint"], "sk-••••AAAA")
        self.assertEqual(saved["credential"]["status"], "UNTESTED")
        self.assertIsNone(saved["credential"]["base_url"])
        self.ok(self.save(self.bob, provider="anthropic", model="claude-haiku-4-5", api_key=KEY_B))

        tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
        self.assertTrue(tested["ok"])
        self.assertEqual(tested["settings"]["credential"]["status"], "OK")
        self.assertEqual(self.fake.calls[-1][1], KEY_A)

        # Each account's request goes out with that account's own key, and no other.
        self.fake.calls.clear()
        a = self.interpret(self.alice)
        b = self.interpret(self.bob)
        c = self.interpret(self.carol)
        self.assertEqual(a["provider"], "openai")
        self.assertEqual(a["actions"][0]["payload"]["title"], "openai:AAAA")
        self.assertEqual(b["provider"], "anthropic")
        self.assertEqual(b["actions"][0]["payload"]["title"], "anthropic:BBBB")
        self.assertEqual(c["provider"], "deterministic-local-v1")
        self.assertEqual([key for _, key in self.fake.calls], [KEY_A, KEY_B])
        self.assertTrue(self.fake.calls[0][0].startswith("https://api.openai.com/v1/"))
        self.assertTrue(self.fake.calls[1][0].startswith("https://api.anthropic.com/v1/"))

        # Applying an LLM proposal creates the task through the normal boundary.
        applied = self.ok(self.client.post("/api/v1/assistant/apply", headers=self._h(self.alice), json={
            "batch_id": a["batch_id"], "action_ids": [a["actions"][0]["id"]], "confirmed_action_ids": [],
            "idempotency_key": "byok-apply-1"}))
        self.assertEqual(applied["results"][0]["entity"]["title"], "openai:AAAA")

        # Bob cannot see or touch Alice's settings; there is no account parameter at all.
        bob_view = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.bob)))
        self.assertEqual(bob_view["credential"]["provider"], "anthropic")
        self.ok(self.client.delete("/api/v1/settings/llm", headers=self._h(self.bob)))
        alice_view = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(alice_view["credential"]["key_hint"], "sk-••••AAAA")

        # Changing only the model keeps the stored key.
        changed = self.ok(self.save(self.alice, provider="openai", model="gpt-5", expected_version=1))
        self.assertEqual(changed["credential"]["model"], "gpt-5")
        self.assertEqual(changed["credential"]["status"], "UNTESTED")
        self.fake.calls.clear()
        self.interpret(self.alice)
        self.assertEqual(self.fake.calls[0][1], KEY_A)
        stale = self.save(self.alice, provider="openai", model="gpt-5", expected_version=1)
        self.assertEqual(stale.status_code, 409)

        # A saved key is never re-pointed at another provider or host without re-entry.
        moved = self.save(self.alice, provider="anthropic", model="claude-haiku-4-5")
        self.assertEqual(moved.status_code, 422)
        with patch("student_execution_os.agent.providers.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            rehosted = self.save(self.alice, provider="openai-compatible", model="m", base_url="https://evil.example/v1")
        self.assertEqual(rehosted.status_code, 422)

        # New key replaces the old one.
        replaced = self.ok(self.save(self.alice, provider="openai", model="gpt-5", api_key="sk-new-key-000000-ZZZZ"))
        self.assertEqual(replaced["credential"]["key_hint"], "sk-••••ZZZZ")

        # Delete → back to the local parser, no outbound call.
        removed = self.ok(self.client.delete("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(removed["source"], "NONE")
        self.assertIsNone(removed["credential"])
        self.fake.calls.clear()
        self.assertEqual(self.interpret(self.alice)["provider"], "deterministic-local-v1")
        self.assertEqual(self.fake.calls, [])

    def test_wrong_key_is_reported_and_capture_falls_back(self):
        self.fake.status[KEY_A] = 401
        self.ok(self.save(self.alice, provider="openai", model="gpt-5-mini", api_key=KEY_A))
        tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
        self.assertFalse(tested["ok"])
        self.assertEqual(tested["status"], "INVALID_KEY")
        self.ok(self.save(self.alice, provider="openai", model="gpt-5-mini", api_key=KEY_A))  # status reset
        preview = self.interpret(self.alice, "купить молоко")
        self.assertTrue(preview["fallback"])
        self.assertEqual(preview["fallback_reason"], "AUTH")
        self.assertEqual(preview["provider"], "deterministic-local-v1")
        self.assertEqual(preview["actions"][0]["payload"]["title"].lower(), "купить молоко")
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(settings["credential"]["status"], "INVALID_KEY")
        caps = self.ok(self.client.get("/api/v1/ask/capabilities", headers=self._h(self.alice)))
        self.assertEqual(caps["credential_status"], "INVALID_KEY")
        self.fake.status[KEY_A] = 404
        tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
        self.assertEqual(tested["status"], "MODEL_NOT_FOUND")

    def test_input_validation(self):
        for payload in (
            {"provider": "gemini", "model": "m", "api_key": KEY_A},
            {"provider": "openai", "model": "", "api_key": KEY_A},
            {"provider": "openai", "model": "gpt 5", "api_key": KEY_A},
            {"provider": "openai", "model": "m", "api_key": "short"},
            {"provider": "openai", "model": "m", "api_key": "has space in key 123"},
            {"provider": "openai", "model": "m"},
            {"provider": "openai-compatible", "model": "m", "api_key": KEY_A},
            {"provider": "openai-compatible", "model": "m", "api_key": KEY_A, "base_url": "http://llm.example/v1"},
        ):
            response = self.save(self.alice, **payload)
            self.assertEqual(response.status_code, 422, payload)
            self.assertNotIn(KEY_A, response.text)
        with patch("student_execution_os.agent.providers.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]):
            metadata = self.save(self.alice, provider="openai-compatible", model="m", api_key=KEY_A,
                                 base_url="https://metadata.example/v1")
        self.assertEqual(metadata.status_code, 422)
        # A malformed body is rejected without echoing what was sent.
        raw = self.client.put("/api/v1/settings/llm", headers=self._h(self.alice), json=[KEY_A])
        self.assertEqual(raw.status_code, 422)
        self.assertNotIn(KEY_A, raw.text)
        self.assertEqual(self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))["source"], "NONE")
        self.assertEqual(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)).status_code, 404)

    def test_openai_compatible_with_public_address(self):
        with patch("student_execution_os.agent.providers.socket.getaddrinfo",
                   return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            saved = self.ok(self.save(self.alice, provider="openai-compatible", model="llama-3.1-8b",
                                      api_key=KEY_A, base_url="https://llm.example/v1/"))
            self.assertEqual(saved["credential"]["base_url"], "https://llm.example/v1")
            self.interpret(self.alice)
        self.assertEqual(self.fake.calls[-1], ("https://llm.example/v1/chat/completions", KEY_A))

    def _public_dns(self):
        return patch("student_execution_os.agent.providers.socket.getaddrinfo",
                     return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.2.161", 443))])

    def test_groq_style_openai_compatible_flow_uses_ai(self):
        with self._public_dns():
            saved = self.ok(self.save(self.alice, provider="openai-compatible", model="openai/gpt-oss-20b",
                                      api_key=KEY_A, base_url="https://api.groq.com/openai/v1"))
            self.assertEqual(saved["source"], "USER_BYOK")
            tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
            self.assertTrue(tested["ok"], tested)
            self.assertEqual(tested["status"], "OK")
            self.assertEqual(tested["checked"], ["key", "endpoint", "model", "format"])
            preview = self.ok(self.client.post("/api/v1/assistant/interpret", headers=self._h(self.alice), json={
                "text": "Задача: купить молоко завтра, 15 минут",
                "context": {"timezone": "Europe/Moscow", "locale": "ru"}}))
        self.assertEqual(preview["credential_source"], "USER_BYOK")
        self.assertEqual(preview["engine"], "AI")
        self.assertFalse(preview["fallback"])
        self.assertEqual(preview["provider"], "openai-compatible")
        self.assertEqual(preview["model"], "openai/gpt-oss-20b")
        self.assertEqual(self.fake.calls[-1], ("https://api.groq.com/openai/v1/chat/completions", KEY_A))
        # Groq's gpt-oss fails JSON mode deterministically at temperature 0 for some inputs.
        self.assertNotIn("temperature", self.fake.bodies[-1])
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(settings["credential"]["status"], "OK")
        self.assertEqual(settings["credential"]["base_url"], "https://api.groq.com/openai/v1")

    def test_provider_refusing_the_server_is_not_reported_as_a_wrong_key(self):
        # Groq answers a request from a region it does not serve with a bare 403, for any key.
        self.fake.status[KEY_A] = 403
        self.fake.error_body = {"error": {"message": "Forbidden"}}
        with self._public_dns():
            self.ok(self.save(self.alice, provider="openai-compatible", model="openai/gpt-oss-20b",
                              api_key=KEY_A, base_url="https://api.groq.com/openai/v1"))
            tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
            self.assertFalse(tested["ok"])
            self.assertEqual((tested["status"], tested["reason"], tested["http_status"]),
                             ("SERVER_BLOCKED", "SERVER_BLOCKED", 403))
            preview = self.interpret(self.alice, "купить молоко")
        self.assertEqual(preview["fallback_reason"], "SERVER_BLOCKED")
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(settings["credential"]["status"], "SERVER_BLOCKED")

    def _replace_key_mid_request(self, who):
        def swap():
            self.fake.during_call = None
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
                LlmCredentialStore(repo).save(who["account"], {"provider": "anthropic", "model": "claude-x",
                                                               "api_key": KEY_B})
        self.fake.during_call = swap

    def test_old_provider_result_cannot_update_replaced_credential(self):
        self.fake.status[KEY_A] = 401
        self.assertEqual(self.ok(self.save(self.alice, provider="openai", model="m", api_key=KEY_A))
                         ["credential"]["version"], 1)
        self._replace_key_mid_request(self.alice)
        tested = self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
        self.assertEqual(tested["status"], "INVALID_KEY")  # the answer about key A itself
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual(settings["credential"]["key_hint"], key_hint(KEY_B))
        self.assertEqual(settings["credential"]["version"], 2)
        self.assertEqual(settings["credential"]["status"], "UNTESTED")
        self.assertIsNone(settings["credential"]["last_checked_at"])

    def test_old_assistant_failure_cannot_update_replaced_credential(self):
        self.fake.status[KEY_A] = 401
        self.ok(self.save(self.alice, provider="openai", model="m", api_key=KEY_A))
        self._replace_key_mid_request(self.alice)
        self.assertEqual(self.interpret(self.alice)["fallback_reason"], "AUTH")
        settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
        self.assertEqual((settings["credential"]["version"], settings["credential"]["status"]), (2, "UNTESTED"))

    def test_test_endpoint_is_rate_limited(self):
        self.ok(self.save(self.alice, provider="openai", model="m", api_key=KEY_A))
        codes = [self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)).status_code for _ in range(7)]
        self.assertEqual(codes[:6], [200] * 6)
        self.assertEqual(codes[6], 429)
        self.ok(self.save(self.bob, provider="openai", model="m", api_key=KEY_B))
        self.assertEqual(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.bob)).status_code, 200)

    def test_key_never_leaves_the_server_in_plaintext(self):
        self.ok(self.save(self.alice, provider="openai", model="gpt-5-mini", api_key=KEY_A))
        self.interpret(self.alice)
        self.ok(self.client.post("/api/v1/settings/llm/test", headers=self._h(self.alice)))
        for path in ("/api/v1/settings/llm", "/api/v1/ask/capabilities", "/api/v1/settings/diagnostics",
                     "/api/v1/account/export", "/api/v1/tasks", "/api/v1/today", "/api/v1/account/deletion-policy"):
            self.ok(self.client.get(path, headers=self._h(self.alice)))
        export = self.ok(self.client.get("/api/v1/account/export", headers=self._h(self.alice)))
        self.assertNotIn("llm_credentials", export["tables"])
        self.assertFalse(export["contract"]["includes_llm_api_keys"])
        self.assertNotIn(KEY_A, self.log.getvalue())
        for file in Path(self.tmp.name).iterdir():
            self.assertNotIn(KEY_A.encode(), file.read_bytes(), file.name)
        backup = Path(self.tmp.name) / "backup.sqlite"
        SQLiteDataLifecycle(self.db, now=lambda: NOW).create_backup(backup)
        self.assertNotIn(KEY_A.encode(), backup.read_bytes())

    def test_stored_ciphertext_is_bound_to_its_account(self):
        self.ok(self.save(self.alice, provider="openai", model="m", api_key=KEY_A))
        conn = sqlite3.connect(self.db)
        try:
            # Simulate a row copied onto Bob's account (e.g. a botched restore or SQL edit).
            conn.execute("INSERT INTO llm_credentials SELECT ?,provider,model,base_url,key_ciphertext,key_nonce,key_id,"
                         "key_hint,status,last_checked_at,created_at,updated_at,version FROM llm_credentials WHERE account_id=?",
                         (self.bob["account"], self.alice["account"]))
            conn.commit()
        finally:
            conn.close()
        self.fake.calls.clear()
        self.assertEqual(self.interpret(self.bob)["provider"], "deterministic-local-v1")
        self.assertEqual(self.fake.calls, [])
        bob = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.bob)))
        self.assertEqual(bob["credential"]["status"], "UNREADABLE")
        self.assertEqual(bob["source"], "NONE")

    def test_without_master_key_keys_cannot_be_stored(self):
        with patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": "", "SEOS_CREDENTIAL_KEY_FILE": ""}):
            settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.alice)))
            self.assertFalse(settings["storage_available"])
            response = self.save(self.alice, provider="openai", model="m", api_key=KEY_A)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"]["code"], "UNSUPPORTED_CAPABILITY")
            self.assertEqual(self.interpret(self.alice)["provider"], "deterministic-local-v1")

    def test_account_deletion_purges_the_credential(self):
        self.ok(self.save(self.alice, provider="openai", model="m", api_key=KEY_A))
        self.ok(self.save(self.bob, provider="anthropic", model="m", api_key=KEY_B))
        policy = self.ok(self.client.get("/api/v1/account/deletion-policy", headers=self._h(self.alice)))
        self.assertEqual(policy["secret_revocation"], "LLM_CREDENTIALS_PURGED_REVOKE_AT_PROVIDER")
        deleted = self.ok(self.client.post("/api/v1/account/delete", headers=self._h(self.alice), json={
            "expected_server_revision": policy["server_revision"], "confirm_login": "alice"}))
        self.assertEqual(deleted["deleted_rows"]["llm_credentials"], 1)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT account_id FROM llm_credentials").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(self.bob["account"],)])

    def test_platform_managed_needs_entitlement_and_no_user_key(self):
        platform_env = {"SEOS_PLATFORM_LLM_PROVIDER": "anthropic", "SEOS_PLATFORM_LLM_MODEL": "claude-platform",
                        "SEOS_PLATFORM_LLM_API_KEY": "platform-secret-key-PPPP"}
        with patch.dict(os.environ, platform_env):
            # Platform credentials alone serve nobody: BYOK is the default model.
            self.assertEqual(self.interpret(self.carol)["provider"], "deterministic-local-v1")
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                LlmCredentialStore(repo).grant_entitlement(self.carol["account"], "ai-plus", NOW + timedelta(days=30))
                LlmCredentialStore(repo).grant_entitlement(self.bob["account"], "ai-plus", NOW - timedelta(days=1))
            settings = self.ok(self.client.get("/api/v1/settings/llm", headers=self._h(self.carol)))
            self.assertEqual(settings["source"], "PLATFORM_MANAGED")
            self.assertTrue(settings["platform_managed"]["available"])
            self.assertNotIn("platform-secret", json.dumps(settings))
            self.fake.calls.clear()
            self.assertEqual(self.interpret(self.carol)["provider"], "anthropic")
            self.assertEqual(self.fake.calls[-1][1], "platform-secret-key-PPPP")
            # Expired entitlement → local parser again.
            self.assertEqual(self.interpret(self.bob)["provider"], "deterministic-local-v1")
            # A user's own key takes precedence over the plan.
            self.ok(self.save(self.carol, provider="openai", model="m", api_key=KEY_A))
            self.assertEqual(self.interpret(self.carol)["provider"], "openai")
            self.assertEqual(self.fake.calls[-1][1], KEY_A)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            LlmCredentialStore(repo).revoke_entitlement(self.carol["account"])


class CredentialCipherTest(unittest.TestCase):
    def test_round_trip_binding_and_rotation(self):
        old = CredentialCipher([os.urandom(32)])
        nonce, ciphertext, key_id = old.encrypt(KEY_A, b"account-a")
        self.assertNotIn(KEY_A.encode(), ciphertext)
        self.assertEqual(old.decrypt(nonce, ciphertext, key_id, b"account-a"), KEY_A)
        with self.assertRaises(CredentialUnreadable):
            old.decrypt(nonce, ciphertext, key_id, b"account-b")
        with self.assertRaises(CredentialUnreadable):
            CredentialCipher([os.urandom(32)]).decrypt(nonce, ciphertext, key_id, b"account-a")
        new_key = os.urandom(32)
        rotated = CredentialCipher([new_key, old._keys[old.active_key_id]])
        self.assertNotEqual(rotated.active_key_id, key_id)
        self.assertEqual(rotated.decrypt(nonce, ciphertext, key_id, b"account-a"), KEY_A)

    def test_rekey_moves_rows_to_the_active_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "rekey.sqlite")
            first = os.urandom(32)
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                repo.create_account("a")
                LlmCredentialStore(repo, CredentialCipher([first]), use_environment=False).save(
                    "a", {"provider": "openai", "model": "m", "api_key": KEY_A})
                rotated = CredentialCipher([os.urandom(32), first])
                store = LlmCredentialStore(repo, rotated, use_environment=False)
                self.assertEqual(store.rekey(), 1)
                self.assertEqual(store.rekey(), 0)
                only_new = CredentialCipher([rotated._keys[rotated.active_key_id]])
                resolved = LlmCredentialStore(repo, only_new, use_environment=False).resolve("a")
                self.assertEqual(resolved.provider.api_key, KEY_A)

    def test_key_file_and_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credential.key"
            path.write_text(f"# active first\n{MASTER}\n", encoding="utf-8")
            with patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": "", "SEOS_CREDENTIAL_KEY_FILE": str(path)}):
                self.assertIsNotNone(CredentialCipher.from_environment())
            with patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": "", "SEOS_CREDENTIAL_KEY_FILE": str(Path(tmp) / "missing")}):
                self.assertIsNone(CredentialCipher.from_environment())
        with self.assertRaises(ValueError):
            CredentialCipher([b"short"])
        self.assertEqual(key_hint("sk-proj-abcdefghijkl1234"), "sk-••••1234")
        self.assertEqual(key_hint("abcdefghijklmnop5678"), "••••5678")
        self.assertEqual(key_hint("short-key"), "••••")


if __name__ == "__main__":
    unittest.main()
