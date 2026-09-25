from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent.nlparse import parse_task

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/nl_capture_cases.json").read_text(encoding="utf-8"))
ZONE = ZoneInfo(FIXTURE["timezone"])
NOW = datetime.fromisoformat(FIXTURE["now"])


def _local(value: str | None) -> str | None:
    return datetime.fromisoformat(value).astimezone(ZONE).strftime("%Y-%m-%d %H:%M") if value else None


def summarize(result: dict) -> dict:
    cutoff = result["actual_cutoff"]
    return {
        "title": result["title"], "estimated_total_effort_minutes": result["estimated_total_effort_minutes"],
        "importance": result["importance"], "category": result["category"],
        "cutoff": cutoff["state"] + (f" {_local(cutoff['at'])}" if cutoff.get("at") else ""),
        "target_at": _local(result["target_at"]), "actionable_from": _local(result["actionable_from"]),
        "remind_at": _local(result["remind_at"]), "splittable": result["splittable"], "unresolved": result["unresolved"],
    }


class NaturalCaptureParserTests(unittest.TestCase):
    def test_fixture_phrases(self):
        for case in FIXTURE["cases"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(summarize(parse_task(case["text"], now=NOW, timezone_name=FIXTURE["timezone"])), case["expect"])

    def test_acceptance_phrase_fields(self):
        result = parse_task("В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно",
                            now=NOW, timezone_name="Europe/Moscow")
        self.assertEqual(result["title"], "Сдать лабораторную по физике")
        self.assertEqual(result["estimated_total_effort_minutes"], 120)
        self.assertEqual(result["importance"], "HIGH")
        self.assertEqual(result["category"], "HOMEWORK")
        # Friday 18:00 Moscow time is 15:00 UTC.
        self.assertEqual(result["actual_cutoff"], {"state": "KNOWN", "at": "2026-09-25T15:00:00+00:00", "boundary": "INCLUSIVE"})
        self.assertEqual(result["unresolved"], [])

    def test_time_zone_changes_the_instant_not_the_wall_clock(self):
        utc = parse_task("сдать отчёт завтра в 18:00", now=NOW, timezone_name="UTC")
        msk = parse_task("сдать отчёт завтра в 18:00", now=NOW, timezone_name="Europe/Moscow")
        self.assertEqual(utc["actual_cutoff"]["at"], "2026-09-24T18:00:00+00:00")
        self.assertEqual(msk["actual_cutoff"]["at"], "2026-09-24T15:00:00+00:00")

    def test_passed_clock_time_rolls_to_tomorrow_and_same_weekday_to_next_week(self):
        evening = datetime(2026, 9, 25, 19, 30, tzinfo=ZONE)  # a Friday evening
        self.assertEqual(_local(parse_task("сдать эссе к шести", now=evening, timezone_name="Europe/Moscow")["actual_cutoff"]["at"]),
                         "2026-09-26 18:00")
        self.assertEqual(_local(parse_task("в пятницу к шести сдать лабу", now=evening, timezone_name="Europe/Moscow")["actual_cutoff"]["at"]),
                         "2026-10-02 18:00")
        morning = datetime(2026, 9, 25, 9, 0, tzinfo=ZONE)
        self.assertEqual(_local(parse_task("в пятницу к шести сдать лабу", now=morning, timezone_name="Europe/Moscow")["actual_cutoff"]["at"]),
                         "2026-09-25 18:00")

    def test_quantities_are_not_clock_times_and_empty_input_is_unresolved(self):
        self.assertIsNone(parse_task("решить в 2 этапа", now=NOW)["actionable_from"])
        self.assertEqual(parse_task("   ", now=NOW), {"title": "", "unresolved": ["title"]})
        described = parse_task("Лаба по физике 2 часа\nвзять тетрадь у Пети", now=NOW)
        self.assertEqual(described["description"], "взять тетрадь у Пети")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_device_parser_matches_server_parser(self):
        """The offline client parser (JS) and the server parser agree on every fixture phrase."""
        output = subprocess.run(
            ["node", str(ROOT / "tests/js/nlparse_cases.mjs")], capture_output=True, text=True, check=True,
            env={**os.environ, "TZ": FIXTURE["timezone"]}, timeout=60,
        ).stdout
        for case, device in zip(FIXTURE["cases"], json.loads(output), strict=True):
            with self.subTest(text=case["text"]):
                self.assertEqual(device, case["expect"])


if __name__ == "__main__":
    unittest.main()
