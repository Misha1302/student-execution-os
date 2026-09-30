from datetime import datetime
import json
from pathlib import Path
import subprocess
import unittest
from zoneinfo import ZoneInfo

from student_execution_os.agent.nlparse import parse_task


class CaptureConversationTest(unittest.TestCase):
    now = datetime(2026, 9, 30, 12, tzinfo=ZoneInfo("Europe/Moscow"))

    def parse(self, text):
        return parse_task(text, now=self.now, timezone_name="Europe/Moscow")

    def test_later_date_replaces_previous_proposition(self):
        for original in ("завтра", "в четверг", "в воскресенье"):
            for replacement in ("в пятницу", "в субботу", "в понедельник"):
                expected = self.parse(f"{replacement} в 18:00 созвон с Ариадной на полчаса")
                for separator in (", ", ". ", "\n"):
                    with self.subTest(original=original, replacement=replacement, separator=separator):
                        actual = self.parse(f"{original} в 18:00 созвон с Ариадной на полчаса{separator}Нет, не {original}, а {replacement}")
                        self.assertEqual(actual, expected)

    def test_later_time_preserves_date_duration_and_title(self):
        for replacement in ("19:30", "20:00", "17:15"):
            self.assertEqual(
                self.parse(f"Завтра в 18:00 созвон с Ариадной на полчаса. Нет, в {replacement}"),
                self.parse(f"Завтра в {replacement} созвон с Ариадной на полчаса"),
            )

    def test_english_corrections(self):
        self.assertEqual(self.parse("Meeting with Ariadne tomorrow at 6. No, Friday instead."),
                         self.parse("Meeting with Ariadne Friday at 6"))
        self.assertEqual(self.parse("Call tomorrow at 6 — actually make it 7:30."),
                         self.parse("Call tomorrow at 19:30"))

    def test_correction_preserves_reminder(self):
        self.assertEqual(
            self.parse("Завтра в 18:00 созвон с Ариадной. Нет, не завтра, а в пятницу. Напомни за час."),
            self.parse("В пятницу в 18:00 созвон с Ариадной. Напомни за час."),
        )

    def test_later_reminder_replaces_only_reminder(self):
        for suffix, expected in (("И напомни за час", 60), ("без напоминания", None)):
            parsed = self.parse("Созвон завтра в 18:00 на полчаса, напомни за 50 минут. " + suffix)
            self.assertEqual(parsed['duration_minutes'], 30)
            self.assertEqual(parsed['remind_before_minutes'], expected)

    def test_whitespace_and_sentence_invariance(self):
        for fragments in (("созвон завтра в 18:00", "на полчаса", "напомни за 50 минут"),
                          ("meeting tomorrow at 18:00", "for half an hour", "remind me 50 minutes before")):
            expected = self.parse(" ".join(fragments))
            for separator in ("\n", ". ", ".\n", "   "):
                for transform in (str.lower, str.upper, str.capitalize):
                    actual = self.parse(separator.join(map(transform, fragments)))
                    self.assertEqual(actual.pop("title").casefold(), expected["title"].casefold())
                    self.assertEqual(actual, {key: value for key, value in expected.items() if key != "title"})

    def test_start_intent_is_not_an_exam_deadline(self):
        for text in ("В субботу утром начать готовиться к экзамену, часа три",
                     "Начать в пятницу вечером готовиться к экзамену, часа три",
                     "В воскресенье утром заняться подготовкой к экзамену, часа три",
                     "Start studying for exam Saturday morning for three hours"):
            with self.subTest(text=text):
                parsed = self.parse(text)
                self.assertEqual(parsed["estimated_total_effort_minutes"], 180)
                self.assertEqual(parsed["actual_cutoff"]["state"], "ABSENT")
                self.assertIsNotNone(parsed["actionable_from"])

    def test_device_server_parity(self):
        phrases = [
            "Завтра в 18:00 созвон с Ариадной, нет, не завтра, а в пятницу",
            "Завтра в 18:00 созвон с Ариадной, нет, в 19:30",
            "Meeting with Ariadne tomorrow at 6. No, Friday instead.",
            "Call tomorrow at 6 — actually make it 7:30.",
            "созвон завтра в 18:00\nна полчаса\nнапомни за 50 минут",
            "В субботу утром начать готовиться к экзамену, часа три",
        ]
        module = Path("src/student_execution_os/web/static/js/nlparse.js").resolve().as_uri()
        script = f"import {{ parseTask }} from '{module}'; const texts = {json.dumps(phrases)}; console.log(JSON.stringify(texts.map(text => parseTask(text, new Date('2026-09-30T12:00:00+03:00')))));"
        result = subprocess.run(["node", "--input-type=module", "-e", script], env={"TZ": "Europe/Moscow"}, capture_output=True, text=True, check=True)
        for text, device in zip(phrases, json.loads(result.stdout)):
            server = self.parse(text)
            for key in ("starts_at", "ends_at", "actionable_from", "target_at", "remind_at"):
                if device.get(key):
                    device[key] = datetime.fromisoformat(device[key].replace("Z", "+00:00")).isoformat()
            self.assertEqual(device, server, text)
