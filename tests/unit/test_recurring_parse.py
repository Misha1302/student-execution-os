"""Recurring requests read the same on the server and on the device (shared fixture)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path

from student_execution_os.agent.recurring import parse_recurring

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/nl_recurring_cases.json").read_text(encoding="utf-8"))


class RecurringParseTests(unittest.TestCase):
    def test_server_parser_matches_fixture(self):
        now = datetime.fromisoformat(FIXTURE["now"])
        for case in FIXTURE["cases"]:
            with self.subTest(text=case["text"]):
                self.assertEqual(parse_recurring(case["text"], now=now, timezone_name=FIXTURE["timezone"]),
                                 case["expected"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_device_parser_matches_fixture(self):
        out = subprocess.run(["node", str(ROOT / "tests/js/recurring_cases.mjs")], capture_output=True, text=True,
                             check=True, env={**os.environ, "TZ": FIXTURE["timezone"]})
        results = json.loads(out.stdout)
        for case, got in zip(FIXTURE["cases"], results):
            with self.subTest(text=case["text"]):
                self.assertEqual(got, case["expected"])

    def test_key_product_phrases(self):
        now = datetime.fromisoformat(FIXTURE["now"])
        read = {case["text"]: case["expected"] for case in FIXTURE["cases"]}
        self.assertEqual(read["Каждый день в 9 напоминай принять витамин D"]["checkin_kind"], "MEDICATION")
        self.assertEqual(read["Каждый день в 22:30 напоминай вынести мусор"]["kind"], "REMINDER_SERIES")
        self.assertEqual(read["Решать по 20 задач матана каждый день"]["target_quantity"], 20)
        self.assertIsNone(read["купить хлеб завтра в 18"])
        self.assertEqual(read["Каждый будний день напоминай взять пропуск"]["unresolved"], ["time"])
        self.assertIsNone(parse_recurring("", now=now))


if __name__ == "__main__":
    unittest.main()
