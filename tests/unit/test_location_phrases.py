"""«Когда приду домой, напомни…» reads the same on the server and on the device."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from student_execution_os.agent.location_phrases import parse_location_trigger, parse_place_create

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/nl_location_trigger_cases.json").read_text(encoding="utf-8"))


class LocationPhraseTests(unittest.TestCase):
    def test_server_reading_matches_fixture(self):
        for case in FIXTURE["cases"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(parse_location_trigger(case["text"], FIXTURE["places"]), case["expected"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_device_reading_matches_fixture(self):
        out = subprocess.run(["node", str(ROOT / "tests/js/location_phrase_cases.mjs")], capture_output=True, text=True,
                             check=True)
        results = json.loads(out.stdout)
        for case, got in zip(FIXTURE["cases"], results["triggers"]):
            with self.subTest(text=case["text"]):
                self.assertEqual(got, case["expected"])
        for case, got in zip(FIXTURE["place_cases"], results["places"]):
            with self.subTest(text=case["text"]):
                self.assertEqual(got, case["expected"])

    def test_place_creation_reading_matches_fixture(self):
        for case in FIXTURE["place_cases"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(parse_place_create(case["text"]), case["expected"])

    def test_places_are_never_invented(self):
        reading = parse_location_trigger("Когда приду в библиотеку напомни взять книгу", FIXTURE["places"])
        self.assertNotIn("place_id", reading)
        self.assertEqual(reading["unresolved"], ["place_id"])
        self.assertEqual(parse_location_trigger("Когда приду домой, напомни разобрать вещи", [])["unresolved"], ["place_id"])


if __name__ == "__main__":
    unittest.main()
