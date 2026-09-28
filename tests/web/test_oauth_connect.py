"""R7: OAuth 2.1 connection of MCP clients (ChatGPT/Codex) -> capability grant.

LOCAL INTEGRATION: the real FastAPI app and SQLite database; nothing is mocked.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from student_execution_os.web.app import AUTHORIZE_LIMITER, REGISTRATION_LIMITER, create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient
from tests.rollback_chain import roll_back_newer_than

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthConnectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "oauth.sqlite")
        self.now = NOW
        for limiter in (REGISTRATION_LIMITER, AUTHORIZE_LIMITER):
            limiter._hits.clear()
            self.addCleanup(limiter._hits.clear)
        self.client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: self.now))
        response = self.client.post("/api/v1/auth/register", json={"login": "alice", "password": "correct horse"})
        body = response.json()
        self.alice = {"Authorization": f"Bearer {body['token']}"}
        self.alice_account = body["user"]["account_id"]

    def register_client(self, redirect=CHATGPT_REDIRECT, name="ChatGPT") -> str:
        response = self.client.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": name,
                                                              "token_endpoint_auth_method": "none"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["client_id"]

    def authorize(self, client_id, challenge, *, redirect=CHATGPT_REDIRECT, scope="tasks:read tasks:write",
                  state="xyz", method="S256", response_type="code",
                  resource="http://testserver/mcp"):
        params = {"response_type": response_type, "client_id": client_id, "redirect_uri": redirect,
                  "code_challenge": challenge, "code_challenge_method": method, "state": state, "scope": scope,
                  "resource": resource}
        return self.client.get("/oauth/authorize", params=params, follow_redirects=False)

    def consent(self, client_id, challenge, scopes=("tasks:read", "tasks:write"), **kwargs) -> dict:
        response = self.authorize(client_id, challenge, **kwargs)
        self.assertEqual(response.status_code, 302, response.text)
        location = response.headers["location"]
        self.assertTrue(location.startswith("/#/connect/"), location)
        request_id = location.rsplit("/", 1)[1]
        approved = self.client.post(f"/api/v1/oauth/requests/{request_id}/approve", headers=self.alice,
                                    json={"scopes": list(scopes)})
        self.assertEqual(approved.status_code, 200, approved.text)
        redirect = urlsplit(approved.json()["redirect"])
        return {"request_id": request_id, "redirect": redirect, "query": parse_qs(redirect.query)}

    def token(self, **form):
        form.setdefault("resource", "http://testserver/mcp")
        return self.client.post("/oauth/token", data=form,
                                headers={"Content-Type": "application/x-www-form-urlencoded"})

    def test_discovery_metadata_and_unauthenticated_mcp_points_to_it(self):
        resource = self.client.get("/.well-known/oauth-protected-resource").json()
        self.assertEqual(resource["resource"], "http://testserver/mcp")
        self.assertEqual(resource["authorization_servers"], ["http://testserver"])
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").json(), resource)
        server = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual((server["issuer"], server["code_challenge_methods_supported"],
                          server["token_endpoint_auth_methods_supported"]),
                         ("http://testserver", ["S256"], ["none"]))
        self.assertTrue(server["authorization_response_iss_parameter_supported"])
        self.assertIn("destructive", server["scopes_supported"])
        denied = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(denied.status_code, 401)
        self.assertIn('resource_metadata="http://testserver/.well-known/oauth-protected-resource"',
                      denied.headers["www-authenticate"])

    def test_full_connection_issues_a_revocable_grant_that_works_on_mcp(self):
        client_id = self.register_client()
        verifier, challenge = pkce()
        described_id = self.authorize(client_id, challenge).headers["location"].rsplit("/", 1)[1]
        described = self.client.get(f"/api/v1/oauth/requests/{described_id}", headers=self.alice).json()
        self.assertEqual((described["client_name"], described["redirect_host"], described["requested_scopes"]),
                         ("ChatGPT", "chatgpt.com", ["tasks:read", "tasks:write"]))
        consent = self.consent(client_id, challenge)
        self.assertEqual(f"{consent['redirect'].scheme}://{consent['redirect'].netloc}{consent['redirect'].path}",
                         CHATGPT_REDIRECT)
        self.assertEqual((consent["query"]["state"], consent["query"]["iss"]), (["xyz"], ["http://testserver"]))
        code = consent["query"]["code"][0]
        issued = self.token(grant_type="authorization_code", code=code, redirect_uri=CHATGPT_REDIRECT,
                            client_id=client_id, code_verifier=verifier)
        self.assertEqual(issued.status_code, 200, issued.text)
        self.assertEqual(issued.headers["cache-control"], "no-store")
        body = issued.json()
        self.assertEqual((body["token_type"], body["scope"], body["expires_in"]),
                         ("Bearer", "tasks:read tasks:write", 90 * 86400))
        bearer = {"Authorization": f"Bearer {body['access_token']}"}
        created = self.client.post("/mcp", headers=bearer, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_task", "arguments": {"op_id": "chatgpt-op-001", "title": "Эссе"}}}).json()
        self.assertEqual(created["result"]["structuredContent"]["results"][0]["status"], "APPLIED")
        self.assertEqual([t["title"] for t in self.client.get("/api/v1/tasks", headers=self.alice).json()], ["Эссе"])
        grants = self.client.get("/api/v1/settings/capabilities", headers=self.alice).json()["grants"]
        self.assertEqual([(g["label"], g["scopes"]) for g in grants], [("ChatGPT", ["tasks:read", "tasks:write"])])
        self.client.delete(f"/api/v1/settings/capabilities/{grants[0]['id']}", headers=self.alice)
        self.assertEqual(self.client.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 2,
                                                                         "method": "ping"}).status_code, 401)

    def test_user_narrows_scopes_and_codex_loopback_redirect(self):
        loopback = "http://127.0.0.1:47123/callback"
        client_id = self.register_client(redirect=loopback, name="Codex")
        verifier, challenge = pkce()
        consent = self.consent(client_id, challenge, scopes=("tasks:read",), redirect=loopback,
                               scope="tasks:read tasks:write destructive")
        body = self.client.post("/oauth/token", json={
            "grant_type": "authorization_code", "code": consent["query"]["code"][0], "redirect_uri": loopback,
            "client_id": client_id, "code_verifier": verifier,
            "resource": "http://testserver/mcp"}).json()
        self.assertEqual(body["scope"], "tasks:read")
        bearer = {"Authorization": f"Bearer {body['access_token']}"}
        denied = self.client.post("/api/v1/ext/operations", headers=bearer, json={"operations": [
            {"op_id": "codex-op-0001", "type": "task.create", "entity_id": "task-codex-001", "payload": {"title": "x"}}]})
        self.assertEqual(denied.status_code, 403)

    def test_code_is_bound_to_verifier_client_redirect_single_use_and_short_lived(self):
        client_id = self.register_client()
        other = self.register_client(name="Other")
        verifier, challenge = pkce()
        code = self.consent(client_id, challenge)["query"]["code"][0]
        base = {"grant_type": "authorization_code", "code": code, "redirect_uri": CHATGPT_REDIRECT,
                "client_id": client_id, "code_verifier": verifier}
        for change in ({"code_verifier": pkce()[0]}, {"client_id": other},
                       {"redirect_uri": CHATGPT_REDIRECT + "/x"}, {"code": "nope"},
                       {"grant_type": "refresh_token"}, {"code_verifier": "short"},
                       {"resource": "https://other.example/mcp"}):
            response = self.token(**{**base, **change})
            self.assertEqual(response.status_code, 400, change)
            self.assertIn(response.json()["error"],
                          {"invalid_grant", "unsupported_grant_type", "invalid_request", "invalid_target"})
        missing_resource = self.client.post("/oauth/token", data=base,
                                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((missing_resource.status_code, missing_resource.json()["error"]),
                         (400, "invalid_target"))
        first = self.token(**base)
        self.assertEqual(first.status_code, 200)
        leaked = {"Authorization": f"Bearer {first.json()['access_token']}"}
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=leaked).status_code, 200)
        replay = self.token(**base)
        self.assertEqual((replay.status_code, replay.json()["error"]), (400, "invalid_grant"))
        # Replaying a code revokes the grant it produced (RFC 6749 §4.1.2 / OAuth 2.1).
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=leaked).status_code, 401)
        # An approved code expires after five minutes.
        verifier2, challenge2 = pkce()
        code2 = self.consent(client_id, challenge2)["query"]["code"][0]
        self.now = NOW + timedelta(minutes=6)
        late = self.token(**{**base, "code": code2, "code_verifier": verifier2})
        self.assertEqual(late.json()["error"], "invalid_grant")

    def test_authorization_request_validation(self):
        client_id = self.register_client()
        _verifier, challenge = pkce()
        # Untrusted client or redirect: an error page, never a redirect.
        for response in (self.authorize("unknown", challenge),
                         self.authorize(client_id, challenge, redirect="https://evil.example/cb")):
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("location", response.headers)
        cases = (({"method": "plain"}, "invalid_request"), ({"challenge": "short"}, "invalid_request"),
                 ({"response_type": "token"}, "unsupported_response_type"),
                 ({"scope": "tasks:read root"}, "invalid_scope"),
                 ({"resource": "https://other.example/mcp"}, "invalid_target"))
        for change, error in cases:
            args = {"challenge": change.pop("challenge", challenge), **change}
            response = self.authorize(client_id, args.pop("challenge"), **args)
            self.assertEqual(response.status_code, 302, change)
            query = parse_qs(urlsplit(response.headers["location"]).query)
            self.assertTrue(response.headers["location"].startswith(CHATGPT_REDIRECT))
            self.assertEqual((query["error"], query["state"]), ([error], ["xyz"]))
        missing_resource = self.client.get("/oauth/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CHATGPT_REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        }, follow_redirects=False)
        self.assertEqual(parse_qs(urlsplit(missing_resource.headers["location"]).query)["error"],
                         ["invalid_target"])
        # No scope requested: read-only by default.
        request_id = self.authorize(client_id, challenge, scope="").headers["location"].rsplit("/", 1)[1]
        self.assertEqual(self.client.get(f"/api/v1/oauth/requests/{request_id}", headers=self.alice).json()
                         ["requested_scopes"], ["calendar:read", "notes:read", "reminders:read", "tasks:read",
                                                "today:read"])

    def test_consent_needs_a_session_and_can_be_declined_once(self):
        client_id = self.register_client()
        _verifier, challenge = pkce()
        request_id = self.authorize(client_id, challenge).headers["location"].rsplit("/", 1)[1]
        self.assertEqual(self.client.get(f"/api/v1/oauth/requests/{request_id}").status_code, 401)
        self.assertEqual(self.client.post(f"/api/v1/oauth/requests/{request_id}/approve",
                                          json={"scopes": ["tasks:read"]}).status_code, 401)
        denied = self.client.post(f"/api/v1/oauth/requests/{request_id}/deny", headers=self.alice).json()
        query = parse_qs(urlsplit(denied["redirect"]).query)
        self.assertEqual((query["error"], query["state"]), (["access_denied"], ["xyz"]))
        again = self.client.post(f"/api/v1/oauth/requests/{request_id}/approve", headers=self.alice,
                                 json={"scopes": ["tasks:read"]})
        self.assertEqual(again.status_code, 404)
        # A pending request expires after ten minutes.
        stale = self.authorize(client_id, challenge).headers["location"].rsplit("/", 1)[1]
        self.now = NOW + timedelta(minutes=11)
        self.assertEqual(self.client.get(f"/api/v1/oauth/requests/{stale}", headers=self.alice).status_code, 404)

    def test_registration_rules(self):
        for uris in ([], ["http://evil.example/cb"], ["https://ok.example/cb#frag"], ["javascript:alert(1)"],
                     ["https://user:pw@ok.example/cb"], "https://ok.example/cb", ["https://a.example"] * 6):
            response = self.client.post("/oauth/register", json={"redirect_uris": uris})
            self.assertEqual(response.status_code, 400, uris)
        REGISTRATION_LIMITER._hits.clear()
        confidential = self.client.post("/oauth/register", json={"redirect_uris": [CHATGPT_REDIRECT],
                                                                  "token_endpoint_auth_method": "client_secret_basic"})
        self.assertEqual(confidential.json()["error"], "invalid_client_metadata")

    def test_unauthenticated_registration_is_rate_limited(self):
        statuses = [self.client.post("/oauth/register", json={"redirect_uris": [CHATGPT_REDIRECT]}).status_code
                    for _ in range(11)]
        self.assertEqual(statuses, [201] * 10 + [429])

    def test_v25_to_v26_upgrade_and_lossless_rollback_keeps_issued_grants(self):
        client_id = self.register_client()
        verifier, challenge = pkce()
        code = self.consent(client_id, challenge)["query"]["code"][0]
        token = self.token(grant_type="authorization_code", code=code, redirect_uri=CHATGPT_REDIRECT,
                           client_id=client_id, code_verifier=verifier).json()["access_token"]
        conn = sqlite3.connect(self.db)
        roll_back_newer_than(conn, 25)
        self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 25)
        self.assertEqual(conn.execute("SELECT count(*) FROM capability_grants").fetchone()[0], 1)
        conn.close()
        from student_execution_os.persistence import SQLiteCanonicalRepository
        from student_execution_os.persistence.sqlite import SCHEMA_VERSION
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            self.assertEqual(repo.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The OAuth-issued grant survived the round trip and still works.
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers={"Authorization": f"Bearer {token}"})
                         .status_code, 200)

    def test_unauthenticated_authorize_is_bounded(self):
        client_id = self.register_client()
        _verifier, challenge = pkce()
        self.assertEqual(self.authorize(client_id, challenge, state="s" * 501).status_code, 400)
        AUTHORIZE_LIMITER._hits.clear()
        statuses = [self.authorize(client_id, challenge).status_code for _ in range(30)]
        self.assertEqual(set(statuses), {302})
        self.assertEqual(self.authorize(client_id, challenge).status_code, 429)
        AUTHORIZE_LIMITER._hits.clear()
        # Expired requests are purged when new ones arrive.
        self.now = NOW + timedelta(minutes=11)
        self.authorize(client_id, challenge)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM oauth_authorizations").fetchone()[0], 1)

    def test_account_deletion_removes_its_authorizations(self):
        client_id = self.register_client()
        _verifier, challenge = pkce()
        self.consent(client_id, challenge)
        from student_execution_os.reliability import SQLiteDataLifecycle
        with sqlite3.connect(self.db) as conn:
            revision = conn.execute("SELECT server_revision FROM accounts WHERE id=?",
                                    (self.alice_account,)).fetchone()[0]
        SQLiteDataLifecycle(self.db, now=lambda: self.now).delete_account(
            self.alice_account, expected_server_revision=revision, confirm_account_id=self.alice_account)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM oauth_authorizations WHERE account_id IS NOT NULL")
                             .fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
