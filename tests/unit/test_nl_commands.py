"""Commands about existing items ("готово эссе", "перенеси созвон на 19:00"): the
server grammar and the device grammar give the same typed actions."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent.commands import match_target, parse_command, reschedule_change

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "tests/fixtures/nl_command_cases.json").read_text(encoding="utf-8"))
ZONE = ZoneInfo(FIXTURE["timezone"])
NOW = datetime.fromisoformat(FIXTURE["now"])


def local(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZONE).strftime("%Y-%m-%d %H:%M")


def summarize(action):
    if action is None:
        return None
    payload = action["payload"]
    out = {"command": action["command"], "target": payload.get("obligation_id") or payload.get("reminder_id")}
    if action["requires_confirmation"]:
        out["confirm"] = True
    if out["target"] is None:
        out["target_text"] = payload.get("target_text")
    for key in ("minutes", "count", "keep_time"):
        if key in payload:
            out[key] = payload[key]
    for key in ("when", "until"):
        if key in payload:
            out[key] = local(payload[key])
    return out


def matches(summary, expect):
    if expect is None or summary is None:
        return summary == expect
    return all(summary.get(key) == value for key, value in expect.items())


class CommandGrammarTests(unittest.TestCase):
    def test_fixture_phrases(self):
        for case in FIXTURE["cases"]:
            with self.subTest(text=case["text"]):
                result = parse_command(case["text"], now=NOW, timezone_name=FIXTURE["timezone"], items=FIXTURE["items"])
                summary = summarize(result)
                self.assertTrue(matches(summary, case["expect"]), f"{summary} != {case['expect']}")
                if result is not None and summary["target"]:
                    item = next(i for i in FIXTURE["items"] if i["id"] == summary["target"])
                    self.assertEqual(result["expected_version"], item["version"])

    def test_ambiguous_fragment_leaves_the_choice_to_the_user(self):
        items = [{"id": "a", "kind": "TASK", "title": "Эссе по истории", "status": "ACTIVE", "version": 1},
                 {"id": "b", "kind": "TASK", "title": "Эссе по праву", "status": "ACTIVE", "version": 1}]
        item, candidates = match_target("эссе", items)
        self.assertIsNone(item)
        self.assertEqual([c["id"] for c in candidates], ["a", "b"])
        action = parse_command("готово эссе", now=NOW, timezone_name="Europe/Moscow", items=items)
        self.assertEqual(action["unresolved_fields"], ["target"])
        self.assertEqual(action["payload"], {"target_text": "эссе"})

    def test_reschedule_keeps_time_of_day_and_event_length_is_the_servers(self):
        task = {"actual_cutoff": {"state": "KNOWN", "at": "2026-09-25T15:00:00+00:00"}}
        op, body = reschedule_change("TASK", task, datetime.fromisoformat("2026-09-28T00:00:00+03:00"), True, ZONE)
        self.assertEqual((op, body["actual_cutoff"]["at"]), ("task.update", "2026-09-28T15:00:00+00:00"))
        op, body = reschedule_change("TASK", {"actual_cutoff": {"state": "ABSENT"}},
                                     datetime.fromisoformat("2026-09-28T09:00:00+03:00"), False, ZONE)
        self.assertEqual((op, body), ("task.defer", {"until": "2026-09-28T06:00:00+00:00"}))
        op, body = reschedule_change("EVENT", {"starts_at": "2026-09-23T15:00:00+00:00"},
                                     datetime.fromisoformat("2026-09-24T00:00:00+03:00"), True, ZONE)
        self.assertEqual((op, body), ("event.update", {"starts_at": "2026-09-24T15:00:00+00:00"}))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_device_grammar_matches_the_server(self):
        out = subprocess.run(["node", str(ROOT / "tests/js/command_cases.mjs")], capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TZ": FIXTURE["timezone"]})
        self.assertEqual(out.returncode, 0, out.stderr)
        device = json.loads(out.stdout)
        for case, js in zip(FIXTURE["cases"], device):
            with self.subTest(text=case["text"]):
                server = parse_command(case["text"], now=NOW, timezone_name=FIXTURE["timezone"], items=FIXTURE["items"])
                self.assertEqual(summarize(js), summarize(server))


if __name__ == "__main__":
    unittest.main()
