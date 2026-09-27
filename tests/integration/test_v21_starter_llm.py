"""Schema-v21 STARTER access, spend protection, and platform credential failover."""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from student_execution_os.agent.credentials import CredentialCipher, LlmCredentialStore
from student_execution_os.agent.usage import StarterQuotaExceeded, StarterQuotaPolicy, StarterUsageStore
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
MASTER = CredentialCipher.generate_key()
PRIMARY = "platform-primary-secret-123456789"
STANDBY = "platform-standby-secret-987654321"
BYOK = "sk-user-owned-123456789"


def _proposal(title: str) -> str:
    return json.dumps({"message": "ok", "actions": [{
        "command": "CREATE_TASK", "payload": {"title": title}, "confidence": 0.9,
        "unresolved_fields": ["estimated_total_effort_minutes", "actual_cutoff"],
        "expected_version": None, "requires_confirmation": False,
    }]})


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.status: dict[str, int] = {}
        self.error: dict[str, object] = {}
        self.raise_for: dict[str, Exception] = {}
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def __call__(self, url, *, headers, json, timeout, follow_redirects):
        key = headers.get("x-api-key") or headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append(key)
        if key in self.raise_for:
            raise self.raise_for[key]
        response = Mock()
        response.headers = {}
        response.status_code = self.status.get(key, 200)
        if response.status_code >= 300:
            response.json.return_value = self.error.get(key, {"error": {"message": "rejected"}})
        else:
            response.json.return_value = {
                "choices": [{"message": {"content": _proposal(f"ai:{key[-7:]}")}}],
                "usage": self.usage,
            }
        return response


class StarterLlmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = str(root / "starter.sqlite")
        self.primary_file = root / "platform-1.key"
        self.standby_file = root / "platform-2.key"
        self.primary_file.write_text(PRIMARY + "\n", encoding="utf-8")
        self.standby_file.write_text(STANDBY + "\n", encoding="utf-8")
        self.fake = FakeProvider()
        self.post = patch("student_execution_os.agent.providers.httpx.post", side_effect=self.fake)
        self.post.start()
        self.log = io.StringIO()
        self.handler = logging.StreamHandler(self.log)
        logging.getLogger().addHandler(self.handler)

    def tearDown(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.post.stop()
        self.tmp.cleanup()

    def env(self, **overrides):
        values = {
            "SEOS_CREDENTIAL_KEY": MASTER,
            "SEOS_STARTER_LLM_ENABLED": "1",
            "SEOS_PLATFORM_LLM_PROVIDER": "openai-compatible",
            "SEOS_PLATFORM_LLM_MODEL": "openai/gpt-oss-20b",
            "SEOS_PLATFORM_LLM_BASE_URL": "https://api.groq.com/openai/v1",
            "SEOS_PLATFORM_LLM_API_KEY_FILES": f"{self.primary_file},{self.standby_file}",
            "SEOS_PLATFORM_LLM_API_KEY": "legacy-must-not-be-used",
            "SEOS_STARTER_LLM_ACCOUNT_REQUEST_LIMIT": "10",
            "SEOS_STARTER_LLM_ACCOUNT_TOKEN_LIMIT": "1000000",
            "SEOS_STARTER_LLM_GLOBAL_REQUEST_LIMIT": "100",
            "SEOS_STARTER_LLM_GLOBAL_TOKEN_LIMIT": "10000000",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=False)

    @staticmethod
    def register(client: TestClient, login: str) -> tuple[dict[str, str], str]:
        response = client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"})
        assert response.status_code == 201, response.text
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}"}, body["user"]["account_id"]

    def interpret(self, client: TestClient, headers: dict[str, str], text: str = "купить молоко"):
        response = client.post("/api/v1/assistant/interpret", headers=headers, json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_new_account_gets_starter_and_feature_flag_off_does_not(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, _ = self.register(client, "starter-user")
            settings = client.get("/api/v1/settings/llm", headers=headers).json()
            self.assertEqual((settings["source"], settings["platform_managed"]["plan"]),
                             ("PLATFORM_MANAGED", "STARTER"))
            self.assertEqual(settings["platform_managed"]["model"], "openai/gpt-oss-20b")
        off_db = str(Path(self.tmp.name) / "off.sqlite")
        with self.env(SEOS_STARTER_LLM_ENABLED="0"):
            client = TestClient(create_app(off_db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "local-user")
            self.assertEqual(client.get("/api/v1/settings/llm", headers=headers).json()["source"], "NONE")
            self.assertEqual(self.interpret(client, headers)["engine"], "LOCAL")
            with sqlite3.connect(off_db) as conn:
                self.assertIsNone(conn.execute("SELECT plan FROM llm_entitlements WHERE account_id=?", (account,)).fetchone())

    def test_byok_wins_does_not_spend_starter_and_delete_restores_starter(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "priority-user")
            self.interpret(client, headers)
            with sqlite3.connect(self.db) as conn:
                before = conn.execute("SELECT request_count,token_count FROM starter_llm_account_usage WHERE account_id=?",
                                      (account,)).fetchone()
            saved = client.put("/api/v1/settings/llm", headers=headers,
                               json={"provider": "openai", "model": "gpt-5-mini", "api_key": BYOK}).json()
            self.assertEqual(saved["source"], "USER_BYOK")
            self.interpret(client, headers)
            with sqlite3.connect(self.db) as conn:
                during = conn.execute("SELECT request_count,token_count FROM starter_llm_account_usage WHERE account_id=?",
                                      (account,)).fetchone()
            self.assertEqual(during, before)
            removed = client.delete("/api/v1/settings/llm", headers=headers).json()
            self.assertEqual(removed["source"], "PLATFORM_MANAGED")
            self.interpret(client, headers)
            with sqlite3.connect(self.db) as conn:
                after = conn.execute("SELECT request_count FROM starter_llm_account_usage WHERE account_id=?",
                                     (account,)).fetchone()[0]
            self.assertEqual(after, before[0] + 1)

    def test_usage_reconciles_and_account_limit_blocks_before_outbound(self):
        with self.env(SEOS_STARTER_LLM_ACCOUNT_REQUEST_LIMIT="1"):
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "quota-user")
            first = self.interpret(client, headers)
            self.assertEqual(first["engine"], "AI")
            calls = len(self.fake.calls)
            second = self.interpret(client, headers)
            self.assertEqual((second["engine"], second["fallback_reason"]), ("LOCAL", "STARTER_QUOTA"))
            self.assertEqual(len(self.fake.calls), calls)
            with sqlite3.connect(self.db) as conn:
                usage = conn.execute("SELECT request_count,token_count FROM starter_llm_account_usage WHERE account_id=?",
                                     (account,)).fetchone()
                reservation = conn.execute(
                    "SELECT prompt_tokens,completion_tokens,total_tokens,status FROM starter_llm_reservations WHERE account_id=?",
                    (account,)).fetchone()
            self.assertEqual(usage, (1, 15))
            self.assertEqual(reservation, (10, 5, 15, "RECONCILED"))

    def test_global_limit_blocks_other_account_before_outbound(self):
        with self.env(SEOS_STARTER_LLM_GLOBAL_REQUEST_LIMIT="1"):
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            first, _ = self.register(client, "global-a")
            second, _ = self.register(client, "global-b")
            self.assertEqual(self.interpret(client, first)["engine"], "AI")
            calls = len(self.fake.calls)
            blocked = self.interpret(client, second)
            self.assertEqual(blocked["fallback_reason"], "STARTER_QUOTA")
            self.assertEqual(len(self.fake.calls), calls)

    def test_account_and_global_token_caps_block_before_outbound(self):
        cases = (
            {"SEOS_STARTER_LLM_ACCOUNT_TOKEN_LIMIT": "1"},
            {"SEOS_STARTER_LLM_ACCOUNT_TOKEN_LIMIT": "1000000",
             "SEOS_STARTER_LLM_GLOBAL_TOKEN_LIMIT": "1"},
        )
        for index, limits in enumerate(cases):
            with self.subTest(limits=limits):
                database = str(Path(self.tmp.name) / f"token-cap-{index}.sqlite")
                with self.env(**limits):
                    client = TestClient(create_app(
                        database, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
                    headers, _ = self.register(client, f"token-cap-{index}")
                    before = len(self.fake.calls)
                    blocked = self.interpret(client, headers)
                    self.assertEqual((blocked["engine"], blocked["fallback_reason"]),
                                     ("LOCAL", "STARTER_QUOTA"))
                    self.assertEqual(len(self.fake.calls), before)

    def test_concurrent_reservations_cannot_cross_global_hard_cap(self):
        policy = StarterQuotaPolicy(enabled=True, account_request_limit=10, account_token_limit=100,
                                    global_request_limit=1, global_token_limit=100)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_account("b")
        barrier = threading.Barrier(2)

        def reserve(account: str) -> str:
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                barrier.wait()
                try:
                    StarterUsageStore(repo, policy).reserve(account, 10)
                    return "reserved"
                except StarterQuotaExceeded:
                    return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, ("a", "b")))
        self.assertEqual(sorted(outcomes), ["blocked", "reserved"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT request_count,token_count FROM starter_llm_global_usage").fetchone(),
                             (1, 10))

    def test_quota_resets_in_the_next_configured_period(self):
        policy = StarterQuotaPolicy(enabled=True, period_seconds=60, account_request_limit=1,
                                    account_token_limit=100, global_request_limit=1, global_token_limit=100)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("periodic")
            StarterUsageStore(repo, policy).reserve("periodic", 10)
            with self.assertRaises(StarterQuotaExceeded):
                StarterUsageStore(repo, policy).reserve("periodic", 10)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW + timedelta(seconds=61))) as repo:
            repo.initialize()
            reservation = StarterUsageStore(repo, policy).reserve("periodic", 10)
        self.assertGreater(reservation.period_start, NOW - timedelta(seconds=60))

    def test_primary_auth_failover_is_bounded_and_semantic_failure_does_not_retry(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, _ = self.register(client, "failover-user")
            self.fake.status[PRIMARY] = 401
            self.fake.error[PRIMARY] = {"error": {"message": "invalid API key", "code": "invalid_api_key"}}
            result = self.interpret(client, headers)
            self.assertEqual(result["engine"], "AI")
            self.assertEqual(self.fake.calls, [PRIMARY, STANDBY])
            self.fake.calls.clear()
            self.fake.status[PRIMARY] = 400
            self.fake.error[PRIMARY] = {"error": {"message": "response_format is unsupported"}}
            semantic = self.interpret(client, headers)
            self.assertEqual((semantic["engine"], semantic["fallback_reason"]), ("LOCAL", "FORMAT"))
            self.assertEqual(self.fake.calls, [PRIMARY])
            for status, reason in ((429, "RATE_LIMITED"), (503, "UPSTREAM")):
                self.fake.calls.clear()
                self.fake.status[PRIMARY] = status
                self.fake.error[PRIMARY] = {"error": {"message": "provider unavailable"}}
                unavailable = self.interpret(client, headers)
                self.assertEqual(unavailable["fallback_reason"], reason)
                self.assertEqual(self.fake.calls, [PRIMARY])
            self.fake.calls.clear()
            self.fake.status[PRIMARY] = 200
            self.fake.raise_for[PRIMARY] = httpx.ConnectError("network down")
            network = self.interpret(client, headers)
            self.assertEqual(network["fallback_reason"], "NETWORK")
            self.assertEqual(self.fake.calls, [PRIMARY])

    def test_platform_secrets_never_enter_api_log_database_or_export(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, _ = self.register(client, "secret-user")
            responses = [client.get("/api/v1/settings/llm", headers=headers),
                         client.get("/api/v1/ask/capabilities", headers=headers),
                         client.get("/api/v1/settings/diagnostics", headers=headers),
                         client.get("/api/v1/account/export", headers=headers)]
            self.interpret(client, headers)
        for secret in (PRIMARY, STANDBY, "legacy-must-not-be-used"):
            self.assertNotIn(secret, "".join(response.text for response in responses))
            self.assertNotIn(secret, self.log.getvalue())
            self.assertNotIn(secret.encode(), Path(self.db).read_bytes())
        self.assertEqual(self.fake.calls, [PRIMARY])

    def test_account_deletion_clears_account_usage_and_backfill_preserves_entitlement(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "delete-usage")
            self.interpret(client, headers)
            policy = client.get("/api/v1/account/deletion-policy", headers=headers).json()
            deleted = client.post("/api/v1/account/delete", headers=headers, json={
                "expected_server_revision": policy["server_revision"], "confirm_login": "delete-usage"}).json()
            self.assertEqual(deleted["deleted_rows"]["starter_llm_account_usage"], 1)
            self.assertEqual(deleted["deleted_rows"]["starter_llm_reservations"], 1)
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM starter_llm_account_usage WHERE account_id=?",
                                              (account,)).fetchone()[0], 0)

        backfill_db = str(Path(self.tmp.name) / "backfill.sqlite")
        with SQLiteCanonicalRepository(backfill_db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("existing")
            repo.create_account("missing")
            LlmCredentialStore(repo, use_environment=False).grant_entitlement("existing", "OPERATOR_PLAN")
        with self.env():
            create_app(backfill_db, account_id="existing", principal_id="u", now=lambda: NOW)
        with sqlite3.connect(backfill_db) as conn:
            rows = dict(conn.execute("SELECT account_id,plan FROM llm_entitlements"))
        self.assertEqual(rows, {"existing": "OPERATOR_PLAN", "missing": "STARTER"})


if __name__ == "__main__":
    unittest.main()
