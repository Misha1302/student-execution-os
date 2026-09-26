"""Collaborative groups over the real HTTP API in session mode (several registered users)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def at(days: int, hours: int = 0) -> str:
    return (NOW + timedelta(days=days, hours=hours)).isoformat()


class GroupsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "groups.sqlite")
        self.client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
        self.tokens = {}
        for login in ("starosta", "student", "stranger"):
            response = self.client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"})
            self.assertEqual(response.status_code, 201, response.text)
            self.tokens[login] = response.json()["token"]
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, who: str, method: str, path: str, body: dict | None = None, *, key: str | None = None, headers=None):
        self.n += 1
        all_headers = {"Authorization": f"Bearer {self.tokens[who]}", **(headers or {})}
        if method in ("POST", "PATCH", "DELETE"):
            all_headers.setdefault("Idempotency-Key", key or f"mutation-{self.n:06d}")
        return self.client.request(method, path, json=body if method != "GET" and method != "DELETE" else None,
                                   headers=all_headers)

    def make_group(self) -> tuple[str, str]:
        group = self.call("starosta", "POST", "/api/v1/groups", {"name": "ПИ-261"}).json()["entity"]
        invite = self.call("starosta", "POST", f"/api/v1/groups/{group['id']}/invites", {"kind": "LINK"})
        self.assertEqual(invite.status_code, 201, invite.text)
        token = invite.json()["entity"]["token"]
        joined = self.call("student", "POST", "/api/v1/groups/join", {"token": token})
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertTrue(joined.json()["entity"]["subscription_setup"])
        return group["id"], token

    def publish(self, group_id: str, **extra) -> dict:
        body = {"title": "Контрольная №2", "event_kind": "CONTROL_WORK", "starts_at": at(14), "ends_at": at(14, 1), **extra}
        response = self.call("starosta", "POST", f"/api/v1/groups/{group_id}/events", body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["entity"]

    def test_status_codes_and_machine_readable_errors(self):
        group_id, _ = self.make_group()
        event = self.publish(group_id)
        # member without capability: 403 with a code
        forbidden = self.call("student", "PATCH", f"/api/v1/groups/{group_id}/events/{event['id']}",
                              {"expected_version": 1, "title": "x"})
        self.assertEqual((forbidden.status_code, forbidden.json()["error"]["code"]), (403, "FORBIDDEN"))
        # outsider: 404, the group's existence is not revealed
        for path in (f"/api/v1/groups/{group_id}", f"/api/v1/groups/{group_id}/events",
                     f"/api/v1/groups/{group_id}/members", f"/api/v1/me/shared-events/{event['id']}/state"):
            self.assertEqual(self.call("stranger", "GET", path).status_code, 404, path)
        # version conflict: 409 with current_version
        self.call("starosta", "PATCH", f"/api/v1/groups/{group_id}/events/{event['id']}", {"expected_version": 1, "location": "R301"})
        stale = self.call("starosta", "PATCH", f"/api/v1/groups/{group_id}/events/{event['id']}", {"expected_version": 1, "location": "R1"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual((stale.json()["error"]["code"], stale.json()["error"]["current_version"]), ("VERSION_CONFLICT", 2))
        # If-Match supplies the expected version
        ok = self.call("starosta", "PATCH", f"/api/v1/groups/{group_id}/events/{event['id']}", {"location": "R401"},
                       headers={"If-Match": '"2"'})
        self.assertEqual((ok.status_code, ok.json()["entity"]["version"]), (200, 3))
        # a mutation without an idempotency key is refused
        response = self.client.post(f"/api/v1/groups/{group_id}/events", json={"title": "x"},
                                    headers={"Authorization": f"Bearer {self.tokens['starosta']}"})
        self.assertEqual(response.status_code, 422)

    def test_idempotency_key_replay_and_proposal_flow(self):
        group_id, _ = self.make_group()
        payload = {"payload": {"kind": "SHARED_EVENT", "title": "Квиз", "event_kind": "QUIZ", "starts_at": at(5), "ends_at": at(5, 1)}}
        proposal = self.call("student", "POST", f"/api/v1/groups/{group_id}/proposals", payload).json()["entity"]
        self.assertEqual(self.call("starosta", "GET", f"/api/v1/groups/{group_id}/events").json()["items"], [])
        first = self.call("starosta", "POST", f"/api/v1/groups/{group_id}/proposals/{proposal['id']}/approve",
                          {"expected_version": 1}, key="approve-key-1")
        again = self.call("starosta", "POST", f"/api/v1/groups/{group_id}/proposals/{proposal['id']}/approve",
                          {"expected_version": 1}, key="approve-key-1")
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(first.json()["entity"]["approved_entity_id"], again.json()["entity"]["approved_entity_id"])
        events = self.call("student", "GET", f"/api/v1/groups/{group_id}/events").json()["items"]
        self.assertEqual(len(events), 1)
        # the member sees only their own proposals, moderators see all
        self.assertEqual(self.call("student", "GET", f"/api/v1/groups/{group_id}/proposals?status=all").json()["items"][0]["id"],
                         proposal["id"])

    def test_idor_entity_must_belong_to_the_group_in_the_path(self):
        group_a, _ = self.make_group()
        other = self.call("stranger", "POST", "/api/v1/groups", {"name": "Чужая"}).json()["entity"]
        foreign = self.call("stranger", "POST", f"/api/v1/groups/{other['id']}/events",
                            {"title": "x", "starts_at": at(2), "ends_at": at(2, 1)}).json()["entity"]
        # the starosta owns group A but addresses an event of group B through A's path
        response = self.call("starosta", "POST", f"/api/v1/groups/{group_a}/events/{foreign['id']}/cancel", {"expected_version": 1})
        self.assertEqual(response.status_code, 404)
        response = self.call("starosta", "GET", f"/api/v1/groups/{group_a}/events/{foreign['id']}")
        self.assertEqual(response.status_code, 404)

    def test_personal_routes_are_separate_and_private(self):
        group_id, _ = self.make_group()
        event = self.publish(group_id)
        patched = self.call("student", "PATCH", f"/api/v1/me/shared-events/{event['id']}/state",
                            {"attendance_override": "OPTIONAL"})
        self.assertEqual(patched.json()["entity"]["attendance"]["effective"], "OPTIONAL")
        # Nothing of it through the group API, even for the owner.
        for path in (f"/api/v1/groups/{group_id}/events/{event['id']}", f"/api/v1/groups/{group_id}/members",
                     f"/api/v1/groups/{group_id}/audit"):
            self.assertNotIn("OPTIONAL", self.call("starosta", "GET", path).text, path)
        self.assertEqual(self.call("starosta", "GET", f"/api/v1/me/shared-events/{event['id']}/state").json()["attendance"]["effective"],
                         "REQUIRED")
        prefs = self.call("student", "PATCH", f"/api/v1/me/groups/{group_id}/preferences", {"show_announcements": False})
        self.assertFalse(prefs.json()["entity"]["show_announcements"])
        self.assertNotIn("show_announcements\": false", self.call("starosta", "GET", f"/api/v1/groups/{group_id}/members").text)
        shared = self.call("student", "GET", "/api/v1/me/shared").json()
        self.assertEqual([g["id"] for g in shared["groups"]], [group_id])
        self.assertEqual([i["id"] for i in shared["items"]], [event["id"]])

    def test_cursor_pagination_is_stable(self):
        group_id, _ = self.make_group()
        ids = [self.publish(group_id, title=f"Событие {n}", event_kind="OTHER", starts_at=at(1 + n), ends_at=at(1 + n, 1))["id"]
               for n in range(5)]
        seen, cursor = [], None
        while True:
            page = self.call("student", "GET", f"/api/v1/groups/{group_id}/events?limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
            seen += [item["id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(seen, ids)
        self.assertEqual(self.call("student", "GET", f"/api/v1/groups/{group_id}/events?cursor=%%%").status_code, 422)

    def test_invite_revoke_and_leave_and_members(self):
        group_id, token = self.make_group()
        invites = self.call("starosta", "GET", f"/api/v1/groups/{group_id}/invites").json()["items"]
        self.assertNotIn(token, str(invites))
        self.assertEqual(self.call("student", "GET", f"/api/v1/groups/{group_id}/invites").status_code, 403)
        revoke = self.call("starosta", "POST", f"/api/v1/groups/{group_id}/invites/{invites[0]['id']}/revoke", {"expected_version": 1})
        self.assertEqual(revoke.status_code, 200)
        refused = self.call("stranger", "POST", "/api/v1/groups/join", {"token": token})
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (422, "INVITE_UNAVAILABLE"))
        members = self.call("student", "GET", f"/api/v1/groups/{group_id}/members").json()["items"]
        self.assertEqual(sorted(m["display_name"] for m in members), ["starosta", "student"])
        self.assertEqual(self.call("student", "POST", f"/api/v1/groups/{group_id}/leave", {}).status_code, 200)
        self.assertEqual(self.call("student", "GET", f"/api/v1/groups/{group_id}").status_code, 404)
        owner_leave = self.call("starosta", "POST", f"/api/v1/groups/{group_id}/leave", {})
        self.assertEqual(owner_leave.json()["error"]["code"], "OWNER_CANNOT_LEAVE")

    def test_sync_endpoint_refuses_group_wide_ops_but_takes_personal_ones(self):
        group_id, _ = self.make_group()
        event = self.publish(group_id)
        response = self.call("starosta", "POST", "/api/v1/sync", {"operations": [
            {"op_id": "queued-cancel-01", "type": "shared_event.cancel", "entity_id": event["id"], "payload": {"expected_version": 1}}]})
        self.assertEqual(response.json()["results"][0]["code"], "ONLINE_ONLY")
        response = self.call("student", "POST", "/api/v1/sync", {"operations": [
            {"op_id": "queued-mute-001", "type": "shared_event_state.update", "entity_id": event["id"], "payload": {"muted": True}}]})
        self.assertEqual(response.json()["results"][0]["status"], "APPLIED")

    def test_plan_shows_the_attended_group_event_as_busy_time(self):
        group_id, _ = self.make_group()
        self.publish(group_id, starts_at=at(0, 3), ends_at=at(0, 4))
        plan = self.call("student", "GET", "/api/v1/plan/agenda?days=2").json()["plan"]
        self.assertEqual([s["label"] for s in plan["shared_events"]], ["ПИ-261: Контрольная №2"])


if __name__ == "__main__":
    unittest.main()
