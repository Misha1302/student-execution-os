"""The unified agenda/search projection: tasks, events and reminders in one list,
canonical entities untouched, and the same result on the server and the device."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

from student_execution_os.web.commitments import commitments

ROOT = Path(__file__).resolve().parents[2]
F = json.loads((ROOT / "tests/fixtures/commitments_cases.json").read_text(encoding="utf-8"))
NOW = datetime.fromisoformat(F["now"])


def run(place, query):
    items = commitments(tasks=F["tasks"], events=F["events"], reminders=F["reminders"], now=NOW, place=place, query=query,
                        shared=F["shared"])
    return [{k: v for k, v in item.items() if k != "entity"} for item in items]


class CommitmentsTest(unittest.TestCase):
    def ids(self, place=None, query=""):
        return [item["id"] for item in run(place, query)]

    def test_places_order_and_search(self):
        self.assertEqual(self.ids("open"), ["e-call", "r-bread", "t-read", "sev-kr", "t-essay", "sob-reg", "t-milk", "san-zoom"])
        self.assertEqual(self.ids("done"), ["sob-lab", "e-past", "r-wake", "t-done"])  # newest first
        self.assertEqual(self.ids("archive"), ["t-nope", "sev-cancel", "e-cancel", "t-arch"])
        self.assertEqual(self.ids(query="купить"), ["r-bread", "t-milk"])
        self.assertEqual(self.ids(query="ГЛАВА 2"), ["t-essay"])
        by_id = {item["id"]: item for item in run(None, "")}
        self.assertEqual((by_id["t-essay"]["at_kind"], by_id["t-read"]["at_kind"], by_id["t-milk"]["at"]), ("due", "from", None))
        self.assertEqual((by_id["e-call"]["kind"], by_id["e-call"]["ends_at"]), ("EVENT", "2026-09-23T16:00:00+00:00"))
        full = commitments(tasks=F["tasks"], events=F["events"], reminders=F["reminders"], now=NOW)
        self.assertIs(next(i for i in full if i["id"] == "t-essay")["entity"], F["tasks"][0], "the canonical payload is kept as is")

    def test_group_items_keep_typed_fields_and_one_logical_event(self):
        by_id = {item["id"]: item for item in run(None, "")}
        # An annotation of the member's own imported event is not a second row.
        self.assertNotIn("sev-annot", by_id)
        self.assertEqual(by_id["e-call"]["annotations"][0]["id"], "sev-annot")
        self.assertEqual(self.ids("open", "защита"), ["e-call"])
        # Hidden by the member / not wanted in the agenda.
        self.assertNotIn("sev-hidden", by_id)
        self.assertNotIn("san-quiet", by_id)
        kr, lab, zoom = by_id["sev-kr"], by_id["sob-lab"], by_id["san-zoom"]
        self.assertEqual((kr["attendance"], kr["criticality"], kr["importance"], kr["source_label"]),
                         ("REQUIRED", "CRITICAL", None, "ПИ-261"))
        self.assertNotIn("attendance", lab)
        self.assertEqual((lab["at_kind"], lab["place"]), ("due", "done"))  # done in the member's own task
        # An announcement is informational: no invented time, no criticality/attendance.
        self.assertEqual((zoom["at"], zoom["at_kind"], zoom["announcement_importance"]), (None, None, "IMPORTANT"))
        self.assertFalse({"attendance", "criticality"} & set(zoom))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_device_projection_matches_the_server(self):
        out = subprocess.run(["node", str(ROOT / "tests/js/commitment_cases.mjs")], capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TZ": "Europe/Moscow"})
        self.assertEqual(out.returncode, 0, out.stderr)
        device = json.loads(out.stdout)
        for query, js in zip(F["queries"], device):
            with self.subTest(**query):
                self.assertEqual(js, run(query["place"], query["query"]))


if __name__ == "__main__":
    unittest.main()
