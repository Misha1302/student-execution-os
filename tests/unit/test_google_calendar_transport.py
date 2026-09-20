from __future__ import annotations

from io import BytesIO
import json
import urllib.error
import urllib.parse
import unittest

from student_execution_os.connectors import (
    GoogleCalendarHTTPTransport,
    GoogleCalendarInvalidSyncToken,
)


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


class GoogleCalendarHTTPTransportTests(unittest.TestCase):
    def test_events_list_uses_read_transport_contract_without_persisting_token(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return _Response(
                {
                    "items": [],
                    "nextSyncToken": "next-token",
                }
            )

        transport = GoogleCalendarHTTPTransport(
            lambda: "access-secret",
            timeout_seconds=7.5,
            opener=opener,
        )
        page = transport.list_events(
            calendar_id="student@example.com",
            sync_token="old-token",
            page_token="page-2",
        )

        parsed = urllib.parse.urlparse(captured["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["syncToken"], ["old-token"])
        self.assertEqual(query["pageToken"], ["page-2"])
        self.assertEqual(query["showDeleted"], ["true"])
        self.assertEqual(query["maxResults"], ["2500"])
        self.assertNotIn("access-secret", captured["url"])
        self.assertEqual(captured["authorization"], "Bearer access-secret")
        self.assertEqual(captured["timeout"], 7.5)
        self.assertEqual(page.next_sync_token, "next-token")

    def test_http_410_maps_to_invalid_sync_token(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                410,
                "Gone",
                {},
                BytesIO(
                    b'{"error":{"code":410,"message":"fullSyncRequired"}}'
                ),
            )

        transport = GoogleCalendarHTTPTransport(
            lambda: "access-secret",
            opener=opener,
        )
        with self.assertRaises(GoogleCalendarInvalidSyncToken):
            transport.list_events(
                calendar_id="primary",
                sync_token="expired",
                page_token=None,
            )


if __name__ == "__main__":
    unittest.main()
