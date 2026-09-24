from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.asgi_client import TestClient

from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig, hash_password, verify_password


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


TASK = {"title": "Essay", "estimated_total_effort_minutes": 60, "actual_cutoff": {"state": "ABSENT"}}


class AuthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "auth.sqlite")
        self.clock = Clock()
        self.client = self._client(AuthConfig(password_scrypt_n=2**10))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def _client(self, config: AuthConfig) -> TestClient:
        return TestClient(create_app(self.db, auth=config, now=self.clock))

    def _register(self, login: str, password: str = "correct horse") -> dict:
        response = self.client.post("/api/v1/auth/register", json={"login": login, "password": password})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_password_hash_round_trip(self):
        encoded = hash_password("s3cret-pass")
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(verify_password("s3cret-pass", encoded))
        self.assertFalse(verify_password("other-pass", encoded))

    def test_health_is_public_and_reports_session_mode(self):
        body = self.client.get("/api/v1/health").json()
        self.assertEqual(body["auth_mode"], "session")
        self.assertTrue(body["registration_open"])

    def test_data_endpoints_require_session(self):
        for path in ("/api/v1/today", "/api/v1/tasks", "/api/v1/settings/diagnostics"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.json()["error"]["code"], "UNAUTHENTICATED")
        response = self.client.get("/api/v1/tasks", headers=self._auth("forged"))
        self.assertEqual(response.status_code, 401)

    def test_register_login_me_logout(self):
        issued = self._register("  Student@Example.org ")
        self.assertEqual(issued["user"]["login"], "student@example.org")
        me = self.client.get("/api/v1/auth/me", headers=self._auth(issued["token"])).json()
        self.assertEqual(me["login"], "student@example.org")
        self.assertEqual(me["auth_mode"], "session")

        bad = self.client.post("/api/v1/auth/login", json={"login": "student@example.org", "password": "wrong-pass"})
        self.assertEqual(bad.status_code, 401)
        unknown = self.client.post("/api/v1/auth/login", json={"login": "nobody", "password": "wrong-pass"})
        self.assertEqual(unknown.status_code, 401)

        login = self.client.post("/api/v1/auth/login", json={"login": "STUDENT@example.org", "password": "correct horse"})
        self.assertEqual(login.status_code, 200)
        token = login.json()["token"]
        self.assertNotEqual(token, issued["token"])
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(token)).status_code, 200)

        self.client.post("/api/v1/auth/logout", headers=self._auth(token))
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(token)).status_code, 401)
        # Other sessions of the same user stay valid.
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(issued["token"])).status_code, 200)

    def test_registration_validation(self):
        self._register("alice")
        dup = self.client.post("/api/v1/auth/register", json={"login": "ALICE", "password": "another pass"})
        self.assertEqual(dup.status_code, 422)
        short = self.client.post("/api/v1/auth/register", json={"login": "bob", "password": "short"})
        self.assertEqual(short.status_code, 422)
        bad_login = self.client.post("/api/v1/auth/register", json={"login": "a b", "password": "long enough"})
        self.assertEqual(bad_login.status_code, 422)

    def test_token_hash_only_is_persisted(self):
        issued = self._register("alice")
        conn = sqlite3.connect(self.db)
        try:
            rows = [r[0] for r in conn.execute("SELECT token_hash FROM auth_sessions")]
            password_hash = conn.execute("SELECT password_hash FROM auth_users").fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn(issued["token"], rows)
        self.assertNotIn("correct horse", password_hash)

    def test_accounts_are_isolated_between_users(self):
        alice = self._auth(self._register("alice")["token"])
        bob = self._auth(self._register("bob")["token"])
        created = self.client.post("/api/v1/tasks", json=TASK, headers=alice)
        self.assertEqual(created.status_code, 201)
        task_id = created.json()["id"]
        self.assertEqual([t["id"] for t in self.client.get("/api/v1/tasks", headers=alice).json()], [task_id])
        self.assertEqual(self.client.get("/api/v1/tasks", headers=bob).json(), [])
        stolen = self.client.post(f"/api/v1/obligations/{task_id}/complete", json={"expected_version": 1}, headers=bob)
        self.assertEqual(stolen.status_code, 404)
        # Account id in a payload is never honoured.
        self.client.post("/api/v1/tasks", json={**TASK, "account_id": "someone-else"}, headers=bob)
        self.assertEqual(len(self.client.get("/api/v1/tasks", headers=alice).json()), 1)

    def test_session_expiry_and_sliding_renewal(self):
        token = self._register("alice")["token"]
        self.clock.value += timedelta(days=20)
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(token)).status_code, 200)
        self.clock.value += timedelta(days=20)  # renewed at day 20, still valid at day 40
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(token)).status_code, 200)
        self.clock.value += timedelta(days=31)
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self._auth(token)).status_code, 401)

    def test_account_deletion_purges_credentials_and_sessions(self):
        issued = self._register("alice")
        headers = self._auth(issued["token"])
        self.client.post("/api/v1/tasks", json=TASK, headers=headers)
        policy = self.client.get("/api/v1/account/deletion-policy", headers=headers).json()
        self.assertEqual(policy["login_credentials"], "PURGED_IMMEDIATELY_ALL_SESSIONS_INVALIDATED")
        wrong = self.client.post(
            "/api/v1/account/delete",
            json={"expected_server_revision": policy["server_revision"], "confirm_login": "bob"},
            headers=headers,
        )
        self.assertEqual(wrong.status_code, 422)
        deleted = self.client.post(
            "/api/v1/account/delete",
            json={"expected_server_revision": policy["server_revision"], "confirm_login": "alice"},
            headers=headers,
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["deleted_rows"]["auth_users"], 1)
        self.assertEqual(self.client.get("/api/v1/tasks", headers=headers).status_code, 401)
        login = self.client.post("/api/v1/auth/login", json={"login": "alice", "password": "correct horse"})
        self.assertEqual(login.status_code, 401)
        # The login name becomes available again; the new account is a fresh one.
        again = self._register("alice")
        self.assertNotEqual(again["user"]["account_id"], issued["user"]["account_id"])

    def test_export_excludes_credentials(self):
        issued = self._register("alice")
        export = self.client.get("/api/v1/account/export", headers=self._auth(issued["token"])).json()
        self.assertNotIn("auth_users", export["tables"])
        self.assertNotIn("auth_sessions", export["tables"])
        self.assertFalse(export["contract"]["includes_login_credentials_or_sessions"])
        raw = SQLiteDataLifecycle(self.db, now=self.clock).export_account(issued["user"]["account_id"]).to_dict()
        self.assertNotIn(issued["token"], str(raw))

    def test_closed_registration(self):
        self.client.__exit__(None, None, None)
        self.client = self._client(AuthConfig(registration_open=False, password_scrypt_n=2**10))
        self.client.__enter__()
        self.assertFalse(self.client.get("/api/v1/health").json()["registration_open"])
        response = self.client.post("/api/v1/auth/register", json={"login": "alice", "password": "long enough"})
        self.assertEqual(response.status_code, 401)

    def test_login_rate_limit(self):
        self._register("alice")
        for _ in range(8):
            self.client.post("/api/v1/auth/login", json={"login": "alice", "password": "wrong-pass"})
        limited = self.client.post("/api/v1/auth/login", json={"login": "alice", "password": "correct horse"})
        self.assertEqual(limited.status_code, 429)
        self.assertTrue(limited.json()["error"]["retryable"])

    def test_cors_preflight_for_capacitor_origin(self):
        response = self.client.options(
            "/api/v1/tasks",
            headers={
                "Origin": "https://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://localhost")
        evil = self.client.options(
            "/api/v1/tasks",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
        )
        self.assertNotIn("access-control-allow-origin", evil.headers)

    def test_bound_mode_still_rejects_session_endpoints(self):
        from tests.ui_fixture import ACCOUNT, seed_ui_database

        db = str(Path(self.tmp.name) / "bound.sqlite")
        seed_ui_database(db)
        client = TestClient(create_app(db, account_id=ACCOUNT, principal_id="u"))
        self.assertEqual(client.get("/api/v1/health").json()["auth_mode"], "bound")
        self.assertEqual(client.post("/api/v1/auth/login", json={"login": "x", "password": "y"}).status_code, 401)
        self.assertEqual(client.get("/api/v1/auth/me").json()["auth_mode"], "bound")


class LiveClockTest(unittest.TestCase):
    def test_default_clock_is_minute_aligned_so_live_plans_are_provable(self):
        from student_execution_os.web.queries import UiService

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "clock.sqlite")
            client = TestClient(create_app(db, auth=AuthConfig()))
            token = client.post("/api/v1/auth/register", json={"login": "clock", "password": "long enough"}).json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            client.post("/api/v1/tasks", json={**TASK, "actual_cutoff": {"state": "ABSENT"}}, headers=headers)
            today = client.get("/api/v1/today", headers=headers).json()
            self.assertNotIn("UNSUPPORTED_SUB_MINUTE_TIME", today["plan"]["explanations"])
            self.assertEqual(datetime.fromisoformat(today["now"]).second, 0)
            service = UiService(db, account_id="x", principal_id="y")
            self.assertEqual(service._now().microsecond, 0)


if __name__ == "__main__":
    unittest.main()
