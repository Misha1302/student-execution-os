"""Small, defensive HTTP reader for private iCalendar subscription URLs.

Calendar URLs are bearer credentials.  This module therefore never includes a URL,
response body, or underlying client exception in a public error.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from .ical import MAX_ICS_BYTES
from .model import AcademicProviderError


def assert_public_feed_url(url: str, *, resolver: Callable[..., object] = socket.getaddrinfo) -> str:
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
    if not addresses or any(not address.is_global for address in addresses):
        raise AcademicProviderError("calendar URL does not resolve to a public address", "BLOCKED_URL")
    return value


class HttpIcsReader:
    """Fetch at most one complete ICS document, with bounded transient retries."""

    def __init__(
        self,
        url: str,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        resolver: Callable[..., object] = socket.getaddrinfo,
    ) -> None:
        self.url = assert_public_feed_url(url, resolver=resolver)
        self.client_factory = client_factory or (
            lambda: httpx.Client(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=False,
                headers={"User-Agent": "botay-academic-calendar/1"},
            )
        )
        self.attempts = max(1, min(3, attempts))
        self.sleep = sleep
        self.resolver = resolver

    def __call__(self) -> bytes:
        last: AcademicProviderError | None = None
        for attempt in range(self.attempts):
            # Re-resolve before every request to resist DNS rebinding between setup and refresh.
            assert_public_feed_url(self.url, resolver=self.resolver)
            try:
                with self.client_factory() as client, client.stream("GET", self.url) as response:
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
            if not retryable or attempt + 1 >= self.attempts:
                assert last is not None
                raise last
            self.sleep(float(2**attempt))
        raise AcademicProviderError("calendar provider could not be reached", "NETWORK")


__all__ = ["HttpIcsReader", "assert_public_feed_url"]
