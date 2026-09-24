from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class Pass10ConformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "pass10.sqlite")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repository:
            repository.initialize()
            repository.create_account("account")
        self.client = TestClient(create_app(self.database, account_id="account", principal_id="user", now=lambda: NOW))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, title, attendance="REQUIRED", **extra):
        return self.client.post("/api/v1/events", json={
            "title": title, "starts_at": (NOW + timedelta(hours=1)).isoformat(),
            "ends_at": (NOW + timedelta(hours=2)).isoformat(), "attendance_policy": attendance,
            "location_effect": {"kind": "NONE"}, **extra,
        })

    def test_at65_hybrid_requires_selection_and_remote_removes_commute(self):
        created = self.event("Hybrid", location_options=[
            {"id": "remote", "label": "Online", "location_effect": {"kind": "REMOTE"}},
            {"id": "neutral", "label": "TBD", "location_effect": {"kind": "NONE"}},
        ])
        self.assertEqual(created.status_code, 201, created.text)
        event = created.json()
        before = self.client.get("/api/v1/today").json()
        self.assertIn(f"UNSELECTED_HYBRID_LOCATION:{event['id']}", before["travel"]["unknown_reasons"])
        selected = self.client.post(f"/api/v1/events/{event['id']}/location-selection", json={
            "option_id": "remote", "expected_version": event["version"],
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        after = self.client.get("/api/v1/today").json()
        self.assertEqual(after["travel"]["unknown_reasons"], [])
        self.assertFalse(any(block["type"] == "TRAVEL_TRANSITION" for block in after["plan"]["blocks"]))

    def test_at66_optional_event_omission_is_policy_bound_and_explained(self):
        required = self.event("Required")
        optional = self.event("Optional", attendance="OPTIONAL")
        self.assertEqual(required.status_code, 201)
        self.assertEqual(optional.status_code, 201)
        before = self.client.get("/api/v1/today").json()
        self.assertEqual(before["plan"]["feasibility_status"], "UNKNOWN")
        profile = self.client.get("/api/v1/settings/planning-profile").json()
        changed = self.client.patch("/api/v1/settings/planning-profile", json={
            "expected_version": profile["version"], "optional_event_policy": "OMIT_OPTIONAL",
        })
        self.assertEqual(changed.status_code, 200, changed.text)
        after = self.client.get("/api/v1/today").json()
        self.assertEqual(after["plan"]["feasibility_status"], "FEASIBLE")
        self.assertIn(f"OPTIONAL_EVENT_OMITTED:{optional.json()['id']}", after["plan"]["explanations"])
        required_blocks = [block for block in after["plan"]["blocks"] if block["type"] == "EVENT_PROJECTION"]
        self.assertEqual([block["obligation_id"] for block in required_blocks], [required.json()["id"]])


if __name__ == "__main__":
    unittest.main()
