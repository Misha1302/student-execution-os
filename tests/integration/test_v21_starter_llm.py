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
        self.headers: dict[str, dict[str, str]] = {}
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def __call__(self, url, *, headers, json, timeout, follow_redirects, trust_env=True):
        assert trust_env is False, "platform egress must not use ambient proxy variables"
        key = headers.get("x-api-key") or headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append(key)
        if key in self.raise_for:
            raise self.raise_for[key]
        response = Mock()
        response.headers = self.headers.get(key, {})
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

    def test_concurrent_reservations_cannot_cross_account_caps_and_refunds_are_exact(self):
        policy = StarterQuotaPolicy(enabled=True, account_request_limit=3, account_token_limit=1000,
                                    global_request_limit=100, global_token_limit=100000)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
        workers = 8
        barrier = threading.Barrier(workers)

        def reserve(_index: int):
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                store = StarterUsageStore(repo, policy)
                barrier.wait()
                try:
                    reservation = store.reserve("a", 100)
                except StarterQuotaExceeded:
                    return "blocked"
                # Half of the winners get a no-generation refund, half real usage.
                store.reconcile(reservation, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                                if _index % 2 else {"prompt_tokens": 20, "completion_tokens": 10})
                return "reserved"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(reserve, range(workers)))
        self.assertEqual(outcomes.count("reserved"), 3)
        with sqlite3.connect(self.db) as conn:
            requests, tokens = conn.execute(
                "SELECT request_count,token_count FROM starter_llm_account_usage WHERE account_id='a'").fetchone()
            reconciled = conn.execute(
                "SELECT COALESCE(SUM(total_tokens),0),COUNT(*) FROM starter_llm_reservations "
                "WHERE account_id='a' AND status='RECONCILED'").fetchone()
            self.assertEqual(conn.execute("SELECT request_count,token_count FROM starter_llm_global_usage").fetchone(),
                             (requests, tokens))
        self.assertEqual(requests, 3)
        self.assertEqual((tokens, reconciled[1]), (reconciled[0], 3))  # counters equal the ledger exactly

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
            self.assertEqual(self.fake.calls, [PRIMARY, PRIMARY])  # one controlled repair attempt
            for status, reason in ((429, "RATE_LIMITED"), (503, "UPSTREAM")):
                self.fake.calls.clear()
                self.fake.status[PRIMARY] = status
                self.fake.error[PRIMARY] = {"error": {"message": "provider unavailable"}}
                unavailable = self.interpret(client, headers)
                self.assertEqual(unavailable["fallback_reason"], reason)
                self.assertEqual(self.fake.calls, [PRIMARY] if status == 429 else [PRIMARY, PRIMARY])
            self.fake.calls.clear()
            self.fake.status[PRIMARY] = 200
            self.fake.raise_for[PRIMARY] = httpx.ConnectError("network down")
            network = self.interpret(client, headers)
            self.assertEqual(network["fallback_reason"], "NETWORK")
            self.assertEqual(self.fake.calls, [PRIMARY, PRIMARY])

    GROQ_TPM_429 = {"error": {
        "message": "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` service tier "
                   "`on_demand` on tokens per minute (TPM): Limit 8000, Used 7000, Requested 2000. Please try "
                   "again in 7s. Need more tokens? Upgrade to Dev Tier today at "
                   "https://console.groq.com/settings/billing",
        "type": "tokens", "code": "rate_limit_exceeded"}}

    def usage_rows(self, account: str):
        with sqlite3.connect(self.db) as conn:
            usage = conn.execute("SELECT request_count,token_count FROM starter_llm_account_usage "
                                 "WHERE account_id=?", (account,)).fetchone()
            global_usage = conn.execute("SELECT request_count,token_count FROM starter_llm_global_usage").fetchone()
            reservations = conn.execute("SELECT total_tokens,status FROM starter_llm_reservations "
                                        "WHERE account_id=? ORDER BY created_at,id", (account,)).fetchall()
        return usage, global_usage, reservations

    def test_groq_rate_limit_is_not_quota_no_failover_retry_after_and_no_token_charge(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "rate-limited")
            self.fake.status[PRIMARY] = 429
            self.fake.error[PRIMARY] = self.GROQ_TPM_429
            self.fake.headers[PRIMARY] = {"retry-after": "7"}
            result = self.interpret(client, headers)
        # Degraded, labelled as local, never as AI success; the billing upsell link in
        # Groq's message does not make a transient limit an exhausted account.
        self.assertEqual((result["engine"], result["fallback"], result["model"]), ("LOCAL", True, None))
        self.assertEqual((result["fallback_reason"], result["retry_after_seconds"]), ("RATE_LIMITED", 7))
        self.assertEqual(self.fake.calls, [PRIMARY])  # no standby failover on a rate limit
        usage, global_usage, reservations = self.usage_rows(account)
        self.assertEqual((usage, global_usage), ((1, 0), (1, 0)))
        self.assertEqual(reservations, [(0, "RECONCILED")])

    def test_server_blocked_does_not_fail_over_or_drain_tokens(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "blocked")
            self.fake.status[PRIMARY] = 403
            self.fake.error[PRIMARY] = {"error": {"message": "Forbidden"}}
            for _ in range(3):
                result = self.interpret(client, headers)
                self.assertEqual((result["engine"], result["fallback_reason"]), ("LOCAL", "SERVER_BLOCKED"))
        self.assertEqual(self.fake.calls, [PRIMARY] * 3)
        usage, _global, reservations = self.usage_rows(account)
        self.assertEqual(usage, (3, 0))  # attempts are still counted; tokens are not
        self.assertEqual(reservations, [(0, "RECONCILED")] * 3)

    def test_failures_that_may_have_generated_stay_charged(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "charged")
            cases = (
                (None, httpx.ReadTimeout("slow"), "TIMEOUT"),
                (503, None, "UPSTREAM"),
                (400, None, "FORMAT"),
            )
            self.fake.error[PRIMARY] = {"error": {"message": "json_validate_failed", "code": "json_validate_failed"}}
            for status, error, reason in cases:
                self.fake.status[PRIMARY] = status or 200
                self.fake.raise_for.pop(PRIMARY, None)
                if error is not None:
                    self.fake.raise_for[PRIMARY] = error
                result = self.interpret(client, headers)
                self.assertEqual(result["fallback_reason"], reason)
        usage, _global, reservations = self.usage_rows(account)
        self.assertEqual(usage[0], 5)  # timeout once; 5xx retry; format + one repair
        self.assertTrue(all(total is None and status == "RECONCILED" for total, status in reservations))
        self.assertGreater(usage[1], 5 * 1200)  # full conservative reservations remain

    def test_standby_is_used_only_for_credential_failures_and_its_outcome_is_accounted(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "standby")
            self.fake.status[PRIMARY] = 401
            self.fake.error[PRIMARY] = {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}
            self.fake.status[STANDBY] = 429
            self.fake.error[STANDBY] = self.GROQ_TPM_429
            result = self.interpret(client, headers)
            self.assertEqual((result["engine"], result["fallback_reason"]), ("LOCAL", "RATE_LIMITED"))
            self.assertEqual(self.fake.calls, [PRIMARY, STANDBY])
            self.fake.calls.clear()
            self.fake.status[STANDBY] = 200
            ok = self.interpret(client, headers)
            self.assertEqual((ok["engine"], ok["model"]), ("AI", "openai/gpt-oss-20b"))
            self.assertEqual(ok["actions"][0]["payload"]["title"], f"ai:{STANDBY[-7:]}")
            self.assertEqual(self.fake.calls, [PRIMARY, STANDBY])
            self.fake.calls.clear()
            self.fake.status[PRIMARY] = 402
            self.fake.error[PRIMARY] = {"error": {"message": "insufficient_quota", "code": "insufficient_quota"}}
            quota = self.interpret(client, headers)
            self.assertEqual(quota["engine"], "AI")
            self.assertEqual(self.fake.calls, [PRIMARY, STANDBY])
        usage, _global, reservations = self.usage_rows(account)
        self.assertEqual(usage, (3, 30))
        self.assertEqual(sorted(total for total, _ in reservations), [0, 15, 15])

    def test_invalid_or_revoked_byok_degrades_locally_and_never_spends_starter(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "revoked")
            client.put("/api/v1/settings/llm", headers=headers,
                       json={"provider": "openai", "model": "gpt-5-mini", "api_key": BYOK})
            working = self.interpret(client, headers)
            self.assertEqual((working["engine"], working["credential_source"]), ("AI", "USER_BYOK"))
            self.fake.status[BYOK] = 401  # the user revoked the key at the provider
            self.fake.error[BYOK] = {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}
            revoked = self.interpret(client, headers)
            self.assertEqual((revoked["engine"], revoked["fallback_reason"], revoked["credential_source"]),
                             ("LOCAL", "AUTH", "USER_BYOK"))
            settings = client.get("/api/v1/settings/llm", headers=headers).json()
            self.assertEqual((settings["source"], settings["credential"]["status"]), ("USER_BYOK", "INVALID_KEY"))
            self.assertNotIn(BYOK, json.dumps(settings))
        self.assertEqual(self.fake.calls, [BYOK, BYOK])  # never the platform key
        self.assertIsNone(self.usage_rows(account)[0])

    def test_starter_off_switch_disables_platform_path_without_deleting_entitlements(self):
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            headers, account = self.register(client, "switch")
            self.assertEqual(self.interpret(client, headers)["engine"], "AI")
        calls = len(self.fake.calls)
        with self.env(SEOS_STARTER_LLM_ENABLED="0"):
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            login = client.post("/api/v1/auth/login", json={"login": "switch", "password": "correct horse"})
            off_headers = {"Authorization": f"Bearer {login.json()['token']}"}
            off = self.interpret(client, off_headers)
            self.assertEqual((off["engine"], off["credential_source"], off["fallback"]), ("LOCAL", "NONE", False))
            settings = client.get("/api/v1/settings/llm", headers=off_headers).json()
            self.assertFalse(settings["platform_managed"]["entitled"])
        self.assertEqual(len(self.fake.calls), calls)  # no outbound request while off
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT plan FROM llm_entitlements WHERE account_id=?",
                                          (account,)).fetchone(), ("STARTER",))
        with self.env():
            client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
            login = client.post("/api/v1/auth/login", json={"login": "switch", "password": "correct horse"})
            on = self.interpret(client, {"Authorization": f"Bearer {login.json()['token']}"})
            self.assertEqual(on["engine"], "AI")

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
