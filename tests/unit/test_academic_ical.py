from __future__ import annotations

import copy
import datetime as dt
import ipaddress
import os
import socket
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpcore
import httpx
from icalendar import Calendar

from student_execution_os.academic.http import (
    HttpIcsReader,
    PinnedNetworkBackend,
    assert_public_feed_url,
)
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

    def test_utc_recurrence_id_names_the_series_local_instance(self):
        # 10:00 Moscow (UTC+3) written as 07:00Z in RECURRENCE-ID and EXDATE.
        calendar = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            b"BEGIN:VEVENT\r\nUID:seminar\r\nDTSTART;TZID=Europe/Moscow:20261005T100000\r\n"
            b"DTEND;TZID=Europe/Moscow:20261005T113000\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\n"
            b"EXDATE:20261019T070000Z\r\nSUMMARY:Seminar\r\nEND:VEVENT\r\n"
            b"BEGIN:VEVENT\r\nUID:seminar\r\nRECURRENCE-ID:20261012T070000Z\r\n"
            b"DTSTART;TZID=Europe/Moscow:20261012T120000\r\nDTEND;TZID=Europe/Moscow:20261012T133000\r\n"
            b"SUMMARY:Seminar moved\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        series = parse_icalendar(
            calendar, source_system_id="source", default_timezone="Europe/Moscow"
        ).snapshot.series[0]
        from datetime import datetime
        self.assertEqual(series.exdates_local, (datetime(2026, 10, 19, 10, 0),))
        self.assertEqual(len(series.changes), 1)
        self.assertEqual(series.changes[0].recurrence_local, datetime(2026, 10, 12, 10, 0))
        self.assertEqual(series.changes[0].starts_local, datetime(2026, 10, 12, 12, 0))


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
            client_factory=lambda _transport: httpx.Client(transport=httpx.MockTransport(transient)),
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
                    client_factory=lambda _transport: httpx.Client(transport=httpx.MockTransport(reject)),
                )
                with self.assertRaises(AcademicProviderError) as raised:
                    reader()
                self.assertEqual((raised.exception.code, len(attempts)), (code, 1))
                self.assertNotIn("private-bearer-token", str(raised.exception))

    def test_unexpected_transport_error_is_contained_without_url(self):
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.RemoteProtocolError(f"peer closed while reading {request.url}", request=request)

        reader = HttpIcsReader(
            "https://calendar.example/private-bearer-token.ics",
            resolver=PUBLIC_DNS,
            sleep=lambda _seconds: None,
            client_factory=lambda _transport: httpx.Client(transport=httpx.MockTransport(broken)),
        )
        with self.assertRaises(AcademicProviderError) as raised:
            reader()
        self.assertEqual(raised.exception.code, "PROVIDER_PROTOCOL_ERROR")
        self.assertNotIn("private-bearer-token", str(raised.exception))

    def test_download_limit_is_enforced(self):
        reader = HttpIcsReader(
            "https://calendar.example/feed.ics",
            resolver=PUBLIC_DNS,
            client_factory=lambda _transport: httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * (MAX_ICS_BYTES + 1), request=request)
            )),
        )
        with self.assertRaises(AcademicProviderError) as raised:
            reader()
        self.assertEqual(raised.exception.code, "TOO_LARGE")


def _dns(*addresses: str):
    family = lambda value: socket.AF_INET6 if ":" in value else socket.AF_INET
    return lambda *_args, **_kwargs: [
        (family(value), socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value, 443)) for value in addresses
    ]


class _FakeStream(httpcore.NetworkStream):
    def __init__(self, backend: "_RecordingBackend", response: bytes) -> None:
        self.backend = backend
        self.response = response
        self.sent = b""

    def read(self, max_bytes, timeout=None):
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    def write(self, buffer, timeout=None):
        self.sent += buffer
        self.backend.requests.append(self.sent)

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.backend.tls.append((server_hostname, ssl_context.verify_mode, ssl_context.check_hostname))
        return self

    def get_extra_info(self, info):
        return None


class _RecordingBackend(httpcore.NetworkBackend):
    """Stands in for the socket layer: records every TCP destination and TLS name."""

    def __init__(self, *responses: bytes, refuse: tuple[str, ...] = ()) -> None:
        self.responses = list(responses)
        self.refuse = refuse
        self.connects: list[tuple[str, int]] = []
        self.tls: list[tuple[object, object, bool]] = []
        self.requests: list[bytes] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError(f"refused https://calendar.example/private-bearer-token.ics {host}")
        return _FakeStream(self, self.responses.pop(0))


def _http(status: int, body: bytes = b"", extra: str = "") -> bytes:
    head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n{extra}\r\n".encode()
    return head + body


class AcademicDnsPinningTests(unittest.TestCase):
    URL = "https://calendar.example/private-bearer-token.ics"

    def reader(self, resolver, backend, **kwargs) -> HttpIcsReader:
        return HttpIcsReader(self.URL, resolver=resolver, network_backend=backend,
                             sleep=lambda _seconds: None, **kwargs)

    def test_public_address_connects_to_validated_literal_with_original_tls_name_and_host(self):
        backend = _RecordingBackend(_http(200, b"BEGIN:VCALENDAR"))
        self.assertEqual(self.reader(PUBLIC_DNS, backend)(), b"BEGIN:VCALENDAR")
        self.assertEqual(backend.connects, [("93.184.216.34", 443)])
        server_hostname, verify_mode, check_hostname = backend.tls[0]
        self.assertEqual(server_hostname, "calendar.example")
        self.assertEqual((verify_mode, check_hostname), (__import__("ssl").CERT_REQUIRED, True))
        self.assertIn(b"\r\nHost: calendar.example\r\n", backend.requests[-1])

    def test_non_public_answers_are_rejected_before_any_connection(self):
        for answers in (("127.0.0.1",), ("10.1.2.3",), ("192.168.0.10",), ("172.16.5.5",),
                        ("169.254.169.254",), ("::1",), ("fe80::1",), ("fc00::1",),
                        ("::ffff:127.0.0.1",), ("::127.0.0.1",), ("64:ff9b::a00:1",),
                        ("64:ff9b:1::5db8:d822",), ("100.64.0.1",), ("0.0.0.0",),
                        ("93.184.216.34", "10.0.0.1")):
            with self.subTest(answers=answers):
                backend = _RecordingBackend()
                with self.assertRaises(AcademicProviderError) as raised:
                    self.reader(_dns(*answers), backend)()
                self.assertEqual(raised.exception.code, "BLOCKED_URL")
                self.assertEqual(backend.connects, [])

    def test_dns_change_after_validation_cannot_redirect_the_connection(self):
        # One validation per attempt; later answers (a rebinding) are never consulted.
        answers = iter([_dns("93.184.216.34"), _dns("93.184.216.34"), _dns("127.0.0.1")])
        resolver = lambda *args, **kwargs: next(answers)(*args, **kwargs)
        backend = _RecordingBackend(_http(200, b"ok"))
        reader = self.reader(resolver, backend)  # construction validates (answer 1)
        self.assertEqual(reader(), b"ok")         # attempt validates (answer 2), then connects
        self.assertEqual(backend.connects, [("93.184.216.34", 443)])

    def test_real_socket_layer_never_resolves_the_hostname_again(self):
        connected: list[tuple[str, int]] = []

        def create_connection(address, *args, **kwargs):
            connected.append(address)
            raise OSError("stop")

        rebinding = _dns("127.0.0.1")
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}), \
                mock.patch("socket.getaddrinfo", side_effect=rebinding), \
                mock.patch("socket.create_connection", side_effect=create_connection):
            reader = HttpIcsReader(self.URL, resolver=PUBLIC_DNS, attempts=1, sleep=lambda _s: None)
            with self.assertRaises(AcademicProviderError) as raised:
                reader()
        self.assertEqual(raised.exception.code, "NETWORK")
        # Direct to the validated literal: no hostname, no ambient proxy.
        self.assertEqual(connected, [("93.184.216.34", 443)])

    def test_retries_connect_only_to_their_own_validated_set(self):
        answers = iter([_dns("93.184.216.34"), _dns("93.184.216.34"), _dns("151.101.1.69"),
                        _dns("127.0.0.1")])
        resolver = lambda *args, **kwargs: next(answers)(*args, **kwargs)
        backend = _RecordingBackend(_http(503), _http(503))
        with self.assertRaises(AcademicProviderError) as raised:
            self.reader(resolver, backend)()
        self.assertEqual(raised.exception.code, "BLOCKED_URL")
        self.assertEqual(backend.connects, [("93.184.216.34", 443), ("151.101.1.69", 443)])

    def test_connection_failover_stays_inside_the_validated_set_and_is_redacted(self):
        backend = _RecordingBackend(_http(200, b"ok"), refuse=("93.184.216.34",))
        resolver = _dns("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
        self.assertEqual(self.reader(resolver, backend)(), b"ok")
        self.assertEqual(backend.connects, [("93.184.216.34", 443),
                                            ("2606:2800:220:1:248:1893:25c8:1946", 443)])
        refusing = _RecordingBackend(refuse=("93.184.216.34",))
        with self.assertRaises(AcademicProviderError) as raised:
            self.reader(PUBLIC_DNS, refusing, attempts=2)()
        self.assertEqual((raised.exception.code, len(refusing.connects)), ("NETWORK", 2))
        for text in (str(raised.exception), repr(raised.exception)):
            self.assertNotIn("private-bearer-token", text)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_internationalized_hostname_is_pinned_by_its_wire_name(self):
        backend = _RecordingBackend(_http(200, b"ok"))
        reader = HttpIcsReader("https://расписание.вшэ.рф/feed.ics", resolver=PUBLIC_DNS,
                               network_backend=backend, sleep=lambda _s: None)
        self.assertEqual(reader(), b"ok")
        self.assertEqual(backend.connects, [("93.184.216.34", 443)])
        self.assertEqual(backend.tls[0][0], "расписание.вшэ.рф".encode("idna").decode("ascii"))

    def test_nat64_of_a_public_address_is_allowed(self):
        backend = _RecordingBackend(_http(200, b"ok"))
        self.assertEqual(self.reader(_dns("64:ff9b::5db8:d822"), backend)(), b"ok")
        self.assertEqual(backend.connects, [("64:ff9b::5db8:d822", 443)])

    def test_redirect_through_pinned_transport_is_not_followed(self):
        backend = _RecordingBackend(_http(302, extra="Location: https://127.0.0.1/\r\n"))
        with self.assertRaises(AcademicProviderError) as raised:
            self.reader(PUBLIC_DNS, backend)()
        self.assertEqual(raised.exception.code, "BLOCKED_REDIRECT")
        self.assertEqual(backend.connects, [("93.184.216.34", 443)])

    def test_pinned_backend_refuses_other_hosts_ports_and_unvalidated_sets(self):
        backend = PinnedNetworkBackend(
            "calendar.example", (ipaddress.ip_address("93.184.216.34"),), inner=_RecordingBackend()
        )
        for host, port in (("other.example", 443), ("calendar.example", 80), ("127.0.0.1", 443)):
            with self.subTest(host=host, port=port), self.assertRaises(httpcore.ConnectError):
                backend.connect_tcp(host, port)
        self.assertEqual(backend.inner.connects, [])
        with self.assertRaises(httpcore.ConnectError):
            backend.connect_unix_socket("/var/run/docker.sock")
        for addresses in ((), (ipaddress.ip_address("10.0.0.1"),)):
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                PinnedNetworkBackend("calendar.example", addresses)



def _issue_certificate(directory: Path, name: str) -> tuple[Path, Path, Path]:
    """Return (ca.pem, cert.pem, key.pem) for ``name`` signed by a throwaway CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.timezone.utc)
    ca_key, leaf_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "botay test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                         critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(2)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = directory / "ca.pem", directory / "cert.pem", directory / "key.pem"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return paths


class _LoopbackAs(httpcore.NetworkBackend):
    """The test's 'internet': the validated public literal is served on loopback."""

    def __init__(self, public: str, port: int) -> None:
        self.public, self.port, self.inner = public, port, httpcore.SyncBackend()
        self.connects: list[tuple[str, int]] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connects.append((host, port))
        if host != self.public:
            raise httpcore.ConnectError("unexpected destination")
        return self.inner.connect_tcp("127.0.0.1", self.port, timeout=timeout)


class PinnedRealTlsTests(unittest.TestCase):
    def serve(self, certificate_name: str):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ca, cert, key = _issue_certificate(directory, certificate_name)
        seen: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["host"] = self.headers.get("Host")
                body = b"BEGIN:VCALENDAR"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        context.sni_callback = lambda _sock, name, _ctx: seen.__setitem__("sni", name)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client_context = ssl.create_default_context(cafile=str(ca))
        return server.server_address[1], client_context, seen

    def test_certificate_and_sni_are_checked_against_the_original_hostname(self):
        port, client_context, seen = self.serve("calendar.example")
        backend = _LoopbackAs("93.184.216.34", port)
        reader = HttpIcsReader("https://calendar.example/feed.ics", resolver=PUBLIC_DNS,
                               network_backend=backend, ssl_context=client_context, attempts=1)
        self.assertEqual(reader(), b"BEGIN:VCALENDAR")
        self.assertEqual(backend.connects, [("93.184.216.34", 443)])
        self.assertEqual((seen["sni"], seen["host"]), ("calendar.example", "calendar.example"))

    def test_certificate_for_another_name_is_rejected_without_leaking_the_url(self):
        port, client_context, _seen = self.serve("attacker.example")
        reader = HttpIcsReader("https://calendar.example/private-bearer-token.ics", resolver=PUBLIC_DNS,
                               network_backend=_LoopbackAs("93.184.216.34", port),
                               ssl_context=client_context, attempts=1)
        with self.assertRaises(AcademicProviderError) as raised:
            reader()
        self.assertEqual(raised.exception.code, "NETWORK")
        self.assertNotIn("private-bearer-token", repr(raised.exception))

    def test_unverified_tls_contexts_are_refused(self):
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        reader = HttpIcsReader("https://calendar.example/feed.ics", resolver=PUBLIC_DNS,
                               network_backend=_RecordingBackend(), ssl_context=insecure, attempts=1)
        with self.assertRaises(ValueError):
            reader()


if __name__ == "__main__":
    unittest.main()
