from __future__ import annotations

import copy
import socket
import unittest
from pathlib import Path

import httpx
from icalendar import Calendar

from student_execution_os.academic.http import HttpIcsReader, assert_public_feed_url
from student_execution_os.academic.ical import MAX_ICS_BYTES, parse_icalendar
from student_execution_os.academic.model import AcademicProviderError

FIXTURE = Path("tests/fixtures/academic_schedule_realistic.ics")
PUBLIC_DNS = lambda *_args, **_kwargs: [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
]


class ICalendarProviderUnitTests(unittest.TestCase):
    def test_reordered_records_and_exact_duplicates_have_identical_snapshot(self):
        calendar = Calendar.from_ical(FIXTURE.read_bytes())
        reordered = copy.deepcopy(calendar)
        reordered.subcomponents = list(reversed(reordered.subcomponents))
        duplicate = copy.deepcopy(calendar)
        event = next(component for component in duplicate.subcomponents if component.name == "VEVENT")
        duplicate.add_component(copy.deepcopy(event))
        expected = parse_icalendar(
            calendar.to_ical(), source_system_id="source", default_timezone="Europe/Moscow"
        ).snapshot
        self.assertEqual(parse_icalendar(
            reordered.to_ical(), source_system_id="source", default_timezone="Europe/Moscow"
        ).snapshot, expected)
        self.assertEqual(parse_icalendar(
            duplicate.to_ical(), source_system_id="source", default_timezone="Europe/Moscow"
        ).snapshot, expected)

    def test_duplicate_revision_selects_newer_and_rejects_equal_version_conflict(self):
        calendar = Calendar.from_ical(FIXTURE.read_bytes())
        master = next(
            component for component in calendar.walk("VEVENT")
            if str(component.get("UID")) == "course-algorithms-42@example.edu"
            and component.get("RECURRENCE-ID") is None
        )
        newer = copy.deepcopy(master)
        newer["SEQUENCE"] = 99
        newer["SUMMARY"] = "Newest title"
        calendar.add_component(newer)
        result = parse_icalendar(
            calendar.to_ical(), source_system_id="source", default_timezone="Europe/Moscow"
        )
        self.assertEqual(result.snapshot.series[0].title, "Newest title")

        conflict = Calendar.from_ical(FIXTURE.read_bytes())
        master = next(component for component in conflict.walk("VEVENT")
                      if component.get("RRULE") is not None)
        same_version = copy.deepcopy(master)
        same_version["SUMMARY"] = "Conflicting title"
        conflict.add_component(same_version)
        with self.assertRaisesRegex(AcademicProviderError, "conflicting duplicate") as raised:
            parse_icalendar(
                conflict.to_ical(), source_system_id="source", default_timezone="Europe/Moscow"
            )
        self.assertEqual(raised.exception.code, "CONFLICTING_DUPLICATE")

    def test_unsupported_rule_fails_instead_of_approximating_and_partial_is_explicit(self):
        monthly = b"""BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:monthly\r\nDTSTART:20260928T100000\r\nDTEND:20260928T110000\r\nRRULE:FREQ=MONTHLY\r\nSUMMARY:Monthly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"""
        with self.assertRaises(AcademicProviderError) as raised:
            parse_icalendar(monthly, source_system_id="source", default_timezone="Europe/Moscow")
        self.assertEqual(raised.exception.code, "UNSUPPORTED_RRULE")
        partial = parse_icalendar(
            FIXTURE.read_bytes(), source_system_id="source", default_timezone="Europe/Moscow", complete=False
        )
        self.assertFalse(partial.snapshot.complete)


class AcademicHttpUnitTests(unittest.TestCase):
    def test_url_policy_blocks_credentials_ports_and_private_addresses(self):
        for url in (
            "http://calendar.example/feed.ics",
            "https://user:pass@calendar.example/feed.ics",
            "https://calendar.example:8443/feed.ics",
        ):
            with self.subTest(url=url), self.assertRaises(AcademicProviderError) as raised:
                assert_public_feed_url(url, resolver=PUBLIC_DNS)
            self.assertEqual(raised.exception.code, "BLOCKED_URL")
        with self.assertRaises(AcademicProviderError) as raised:
            assert_public_feed_url(
                "https://calendar.example/feed.ics",
                resolver=lambda *_args, **_kwargs: [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
                ],
            )
        self.assertEqual(raised.exception.code, "BLOCKED_URL")

    def test_bounded_retry_redirect_auth_and_secret_redaction(self):
        requests = []

        def transient(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503 if len(requests) < 3 else 200, content=b"calendar", request=request)

        sleeps: list[float] = []
        reader = HttpIcsReader(
            "https://calendar.example/private-bearer-token.ics",
            resolver=PUBLIC_DNS,
            sleep=sleeps.append,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(transient)),
        )
        self.assertEqual(reader(), b"calendar")
        self.assertEqual((len(requests), sleeps), (3, [1.0, 2.0]))

        for status, code in ((302, "BLOCKED_REDIRECT"), (401, "AUTH_REQUIRED")):
            with self.subTest(status=status):
                attempts = []

                def reject(request, status=status):
                    attempts.append(request)
                    return httpx.Response(status, headers={"location": "https://other.example/"}, request=request)

                reader = HttpIcsReader(
                    "https://calendar.example/private-bearer-token.ics",
                    resolver=PUBLIC_DNS,
                    sleep=lambda _seconds: None,
                    client_factory=lambda: httpx.Client(transport=httpx.MockTransport(reject)),
                )
                with self.assertRaises(AcademicProviderError) as raised:
                    reader()
                self.assertEqual((raised.exception.code, len(attempts)), (code, 1))
                self.assertNotIn("private-bearer-token", str(raised.exception))

    def test_download_limit_is_enforced(self):
        reader = HttpIcsReader(
            "https://calendar.example/feed.ics",
            resolver=PUBLIC_DNS,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * (MAX_ICS_BYTES + 1), request=request)
            )),
        )
        with self.assertRaises(AcademicProviderError) as raised:
            reader()
        self.assertEqual(raised.exception.code, "TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
