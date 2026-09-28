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

import socket
import ssl
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpcore
import httpx

from student_execution_os.netguard import (
    IpAddress,
    PinnedNetworkBackend,
    PinnedTransport,
    is_public_address,
    resolve_addresses,
    wire_host,
)

from .ical import MAX_ICS_BYTES
from .model import AcademicProviderError


def assert_public_feed_url(url: str, *, resolver: Callable[..., object] = socket.getaddrinfo) -> str:
    return resolve_public_feed_url(url, resolver=resolver)[0]


def resolve_public_feed_url(
    url: str, *, resolver: Callable[..., object] = socket.getaddrinfo
) -> tuple[str, str, tuple[IpAddress, ...]]:
    """Return ``(url, wire hostname, validated addresses)`` or raise a safe provider error."""
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AcademicProviderError("calendar URL must be a public HTTPS address", "BLOCKED_URL")
    if parsed.port not in (None, 443):
        raise AcademicProviderError("calendar URL must use the standard HTTPS port", "BLOCKED_URL")
    try:
        addresses = resolve_addresses(parsed.hostname, parsed.port or 443, resolver)
    except (OSError, ValueError, TypeError, IndexError) as exc:
        raise AcademicProviderError("calendar host could not be resolved", "NETWORK") from exc
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise AcademicProviderError("calendar URL does not resolve to a public address", "BLOCKED_URL")
    try:
        host = wire_host(value)
    except (httpx.InvalidURL, UnicodeError) as exc:
        raise AcademicProviderError("calendar URL must be a public HTTPS address", "BLOCKED_URL") from exc
    return value, host, addresses


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
    "is_public_address",
    "resolve_public_feed_url",
]
