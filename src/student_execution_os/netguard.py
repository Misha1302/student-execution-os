"""Outbound-connection guard for user-controlled HTTPS addresses.

One owner for "is this a public address" and "connect only where we validated".
A caller resolves the host once, validates the complete answer set, and builds a
``PinnedTransport``: TCP goes only to those literals while TLS SNI, certificate
verification and the ``Host`` header keep the original (IDNA wire) hostname, so a DNS
answer that changes after validation (rebinding) cannot reach a private address.
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable

import httpcore
import httpx

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


def resolve_addresses(host: str, port: int, resolver: Callable[..., object] = socket.getaddrinfo
                      ) -> tuple[IpAddress, ...]:
    """Every answer for ``host``, deterministically ordered. Raises OSError/ValueError."""
    answers = resolver(host, port, type=socket.SOCK_STREAM)
    try:
        addresses = {ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0]) for answer in answers}  # type: ignore[index, union-attr]
    except (TypeError, IndexError) as exc:
        raise ValueError("unexpected resolver answer") from exc
    return tuple(sorted(addresses, key=lambda address: (address.version, int(address))))


def wire_host(url: str) -> str:
    """The exact (IDNA-encoded) name httpcore connects to and sends as SNI."""
    return httpx.URL(url).raw_host.decode("ascii")


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect only to addresses validated for ``hostname``:``port``.

    httpcore passes the *origin* hostname here and separately uses it for TLS
    ``server_hostname``; only the TCP destination is replaced by a validated literal.
    """

    def __init__(
        self,
        hostname: str,
        addresses: tuple[IpAddress, ...],
        *,
        port: int = 443,
        inner: httpcore.NetworkBackend | None = None,
    ) -> None:
        if not addresses or any(not is_public_address(address) for address in addresses):
            raise ValueError("pinned addresses must be validated public addresses")
        self.hostname = hostname.lower().rstrip(".")
        self.port = port
        self.addresses = addresses
        self.inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host.lower().rstrip(".") != self.hostname or port != self.port:
            raise httpcore.ConnectError("destination is not the validated host")
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
        raise httpcore.ConnectError("unix sockets are not permitted destinations")

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
            raise ValueError("pinned TLS must verify the certificate and hostname")
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=1,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend,
        )


__all__ = [
    "IpAddress",
    "PinnedNetworkBackend",
    "PinnedTransport",
    "is_public_address",
    "resolve_addresses",
    "wire_host",
]
