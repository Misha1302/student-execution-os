"""Small, defensive HTTP reader for private iCalendar subscription URLs.

Calendar URLs are bearer credentials.  This module therefore never includes a URL,
response body, or underlying client exception in a public error.

DNS pinning: each attempt resolves the host once, validates the complete address set,
and the transport then connects only to those validated literals.  TLS SNI, certificate
verification and the Host header keep the original hostname, so a DNS answer changed
after validation (rebinding) cannot steer the connection to a private address.  Ambient
``HTTPS_PROXY``-style variables are ignored: a proxy would resolve the name itself.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpcore
import httpx

from .ical import MAX_ICS_BYTES
from .model import AcademicProviderError


IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


def is_public_address(address: IpAddress) -> bool:
    """``is_global``, plus embedded-IPv4 forms that ``ipaddress`` reports as global."""
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address in _NAT64_LOCAL_USE:
            return False
        if address in _NAT64_WELL_KNOWN or address in _IPV4_COMPATIBLE:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF).is_global
    return True


def assert_public_feed_url(url: str, *, resolver: Callable[..., object] = socket.getaddrinfo) -> str:
    return resolve_public_feed_url(url, resolver=resolver)[0]


def resolve_public_feed_url(
    url: str, *, resolver: Callable[..., object] = socket.getaddrinfo
) -> tuple[str, str, tuple[IpAddress, ...]]:
    """Return ``(url, hostname, validated addresses)`` or raise a safe provider error."""
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AcademicProviderError("calendar URL must be a public HTTPS address", "BLOCKED_URL")
    if parsed.port not in (None, 443):
        raise AcademicProviderError("calendar URL must use the standard HTTPS port", "BLOCKED_URL")
    try:
        answers = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}  # type: ignore[index]
    except (OSError, ValueError, TypeError, IndexError) as exc:
        raise AcademicProviderError("calendar host could not be resolved", "NETWORK") from exc
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise AcademicProviderError("calendar URL does not resolve to a public address", "BLOCKED_URL")
    ordered = tuple(sorted(addresses, key=lambda address: (address.version, int(address))))
    try:
        # The exact (IDNA-encoded) name httpcore hands to the network backend and uses for SNI.
        wire_host = httpx.URL(value).raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise AcademicProviderError("calendar URL must be a public HTTPS address", "BLOCKED_URL") from exc
    return value, wire_host, ordered


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect only to addresses validated for ``hostname`` in this attempt.

    httpcore passes the *origin* hostname here and separately uses it for TLS
    ``server_hostname``; only the TCP destination is replaced by a validated literal.
    """

    def __init__(
        self,
        hostname: str,
        addresses: tuple[IpAddress, ...],
        *,
        inner: httpcore.NetworkBackend | None = None,
    ) -> None:
        if not addresses or any(not is_public_address(address) for address in addresses):
            raise ValueError("pinned addresses must be validated public addresses")
        self.hostname = hostname.lower().rstrip(".")
        self.addresses = addresses
        self.inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host.lower().rstrip(".") != self.hostname or port != 443:
            raise httpcore.ConnectError("destination is not the validated calendar host")
        last: Exception | None = None
        for address in self.addresses:
            try:
                return self.inner.connect_tcp(
                    str(address), port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not calendar destinations")

    def sleep(self, seconds: float) -> None:
        self.inner.sleep(seconds)


class PinnedTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` with verified TLS and a pinned network backend."""

    def __init__(
        self, backend: httpcore.NetworkBackend, *, ssl_context: ssl.SSLContext | None = None
    ) -> None:
        super().__init__(verify=True, http1=True, http2=False, retries=0)
        context = ssl_context or httpx.create_ssl_context(verify=True)
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("calendar TLS must verify the certificate and hostname")
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=1,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


def _default_client(transport: httpx.BaseTransport) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        trust_env=False,
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": "botay-academic-calendar/1"},
    )


class HttpIcsReader:
    """Fetch at most one complete ICS document, with bounded transient retries."""

    def __init__(
        self,
        url: str,
        *,
        client_factory: Callable[[httpx.BaseTransport], httpx.Client] = _default_client,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        resolver: Callable[..., object] = socket.getaddrinfo,
        network_backend: httpcore.NetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.url = assert_public_feed_url(url, resolver=resolver)
        # The factory receives the pinned transport and must route requests through it.
        self.client_factory = client_factory
        self.network_backend = network_backend
        self.ssl_context = ssl_context
        self.attempts = max(1, min(3, attempts))
        self.sleep = sleep
        self.resolver = resolver

    def __call__(self) -> bytes:
        last: AcademicProviderError | None = None
        for attempt in range(self.attempts):
            # Resolve + validate once per attempt; the attempt connects only to that set.
            _url, hostname, addresses = resolve_public_feed_url(self.url, resolver=self.resolver)
            transport = PinnedTransport(
                PinnedNetworkBackend(hostname, addresses, inner=self.network_backend),
                ssl_context=self.ssl_context,
            )
            try:
                with self.client_factory(transport) as client, client.stream("GET", self.url) as response:
                    if 300 <= response.status_code < 400:
                        raise AcademicProviderError("calendar redirects are not followed", "BLOCKED_REDIRECT")
                    if response.status_code in (401, 403):
                        raise AcademicProviderError("calendar authorization was rejected", "AUTH_REQUIRED")
                    if response.status_code == 404:
                        raise AcademicProviderError("calendar feed was not found", "NOT_FOUND")
                    if response.status_code == 429:
                        raise AcademicProviderError("calendar provider rate limited the refresh", "RATE_LIMITED")
                    if response.status_code >= 500:
                        raise AcademicProviderError("calendar provider is temporarily unavailable", "PROVIDER_UNAVAILABLE")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise AcademicProviderError("calendar provider rejected the request", "PROVIDER_REJECTED")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ICS_BYTES:
                            raise AcademicProviderError("calendar file is too large", "TOO_LARGE")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except AcademicProviderError as exc:
                last = exc
                retryable = exc.code in {"RATE_LIMITED", "PROVIDER_UNAVAILABLE"}
            except (httpx.TimeoutException, httpx.NetworkError):
                last = AcademicProviderError("calendar provider could not be reached", "NETWORK")
                retryable = True
            except httpx.HTTPError:
                # Protocol/proxy/decoding errors may embed the request URL; never surface them.
                last = AcademicProviderError("calendar provider returned an invalid response", "PROVIDER_PROTOCOL_ERROR")
                retryable = False
            if not retryable or attempt + 1 >= self.attempts:
                assert last is not None
                raise last
            self.sleep(float(2**attempt))
        raise AcademicProviderError("calendar provider could not be reached", "NETWORK")


__all__ = [
    "HttpIcsReader",
    "PinnedNetworkBackend",
    "PinnedTransport",
    "assert_public_feed_url",
    "resolve_public_feed_url",
]
