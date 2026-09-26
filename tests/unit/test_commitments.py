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
    items = commitments(tasks=F["tasks"], events=F["events"], reminders=F["reminders"], now=NOW, place=place, query=query)
    return [{k: v for k, v in item.items() if k != "entity"} for item in items]


class CommitmentsTest(unittest.TestCase):
    def ids(self, place=None, query=""):
        return [item["id"] for item in run(place, query)]

    def test_places_order_and_search(self):
        self.assertEqual(self.ids("open"), ["e-call", "r-bread", "t-read", "t-essay", "t-milk"])
        self.assertEqual(self.ids("done"), ["e-past", "r-wake", "t-done"])  # newest first
        self.assertEqual(self.ids("archive"), ["t-nope", "e-cancel", "t-arch"])
        self.assertEqual(self.ids(query="купить"), ["r-bread", "t-milk"])
        self.assertEqual(self.ids(query="ГЛАВА 2"), ["t-essay"])
        by_id = {item["id"]: item for item in run(None, "")}
        self.assertEqual((by_id["t-essay"]["at_kind"], by_id["t-read"]["at_kind"], by_id["t-milk"]["at"]), ("due", "from", None))
        self.assertEqual((by_id["e-call"]["kind"], by_id["e-call"]["ends_at"]), ("EVENT", "2026-09-23T16:00:00+00:00"))
        full = commitments(tasks=F["tasks"], events=F["events"], reminders=F["reminders"], now=NOW)
        self.assertIs(next(i for i in full if i["id"] == "t-essay")["entity"], F["tasks"][0], "the canonical payload is kept as is")

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
