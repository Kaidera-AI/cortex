"""Egress-guarded HTTP transport for Processing provider adapters.

Stdlib only. Every request passes :meth:`EgressPolicy.check` *before* any
socket is opened: the host must be allowlisted, the scheme must be https
(plain http only for hosts explicitly listed in ``allow_private_for``), and
every address the host resolves to must be public unless the host is
privately allowlisted (SSRF guard, N05). The transport never retries — a
retry here could duplicate a billed effect, so recovery is the queue's job
through the typed outcomes the adapters map onto.

``HttpTransport`` is an async callable so the blocking urllib work runs in a
worker thread via :func:`asyncio.to_thread` and never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

#: Hard cap for one provider response body; embeddings payloads are small and
#: an oversized body is provider misbehaviour, not something to buffer.
DEFAULT_MAX_RESPONSE_BYTES = 8_000_000


class EgressDenied(Exception):
    """A URL failed the egress policy before any request was made."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TransportError(Exception):
    """Base typed transport failure; ``reason`` never contains header values."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TransportTimeout(TransportError):
    """A connect or read operation exceeded its bounded timeout."""


class TransportConnectionError(TransportError):
    """The peer could not be reached or the connection broke mid-response."""


class TransportRedirect(TransportError):
    """A 3xx was received; redirects are disabled so egress stays pinned."""


class TransportResponseTooLarge(TransportError):
    """The response body exceeded ``EgressPolicy.max_response_bytes``."""


def is_restricted_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """True for loopback/private/link-local/multicast/reserved/unspecified."""
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _address_variants(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    mapped = getattr(address, "ipv4_mapped", None)
    return (address, mapped) if mapped is not None else (address,)


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """Allowlist-first egress rule set enforced before any socket is opened.

    ``allow_private_for`` exempts a host (exact, lowercase) from both the
    https-only rule and the restricted-address rule; it never exempts a host
    from the allowlist itself.
    """

    allowlist: tuple[str, ...] = ()
    allow_private_for: tuple[str, ...] = ()
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "allowlist", tuple(host.lower() for host in self.allowlist)
        )
        object.__setattr__(
            self,
            "allow_private_for",
            tuple(host.lower() for host in self.allow_private_for),
        )

    def check(self, url: str) -> None:
        """Raise :class:`EgressDenied` unless *url* may be requested."""
        parts = urlparse.urlsplit(url)
        scheme = parts.scheme.lower()
        try:
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError as exc:
            raise EgressDenied("url has an invalid port") from exc
        if not host:
            raise EgressDenied("url has no host")
        privileged = host in self.allow_private_for
        if scheme != "https" and not (scheme == "http" and privileged):
            display = scheme or "missing"
            raise EgressDenied(
                f"scheme '{display}' is not permitted for host '{host}': https is "
                "required (http only for hosts listed in allow_private_for)"
            )
        if host not in self.allowlist:
            raise EgressDenied(f"host '{host}' is not in the egress allowlist")
        try:
            infos = socket.getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, UnicodeError) as exc:
            raise EgressDenied(f"host '{host}' does not resolve") from exc
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
            except ValueError as exc:
                raise EgressDenied(f"host '{host}' does not resolve") from exc
            if privileged:
                continue
            for candidate in _address_variants(address):
                if is_restricted_address(candidate):
                    raise EgressDenied(
                        f"host '{host}' resolves to restricted address {candidate}; "
                        "it is not listed in allow_private_for"
                    )


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One bounded provider request; ``timeout`` is the caller's budget."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    timeout: float | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One provider response; header keys are normalized to lowercase."""

    status: int
    headers: dict[str, str]
    body: bytes


#: Async transport port. Blocking work must run off the event loop.
HttpTransport = Callable[[HttpRequest], Awaitable[HttpResponse]]


class _RedirectDenied(urlrequest.HTTPRedirectHandler):
    """Redirects are disabled: a moved endpoint must be a config change."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportRedirect(
            f"redirect {code} is disabled by egress policy; update base_url instead"
        )


def _host_port(url: str) -> tuple[str, int]:
    parts = urlparse.urlsplit(url)
    host = parts.hostname or ""
    if not host:
        raise TransportConnectionError("request url has no host")
    try:
        port = parts.port
    except ValueError as exc:
        raise TransportConnectionError("request url has an invalid port") from exc
    return host, port or (443 if parts.scheme.lower() == "https" else 80)


def _read_response(response: Any, policy: EgressPolicy) -> HttpResponse:
    body = response.read(policy.max_response_bytes + 1)
    if len(body) > policy.max_response_bytes:
        raise TransportResponseTooLarge(
            f"response exceeds the {policy.max_response_bytes}-byte cap"
        )
    raw_headers = getattr(response, "headers", None)
    headers = (
        {key.lower(): value for key, value in raw_headers.items()}
        if raw_headers
        else {}
    )
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", 0)
    return HttpResponse(status=int(status), headers=headers, body=body)


def _execute(request: HttpRequest, policy: EgressPolicy) -> HttpResponse:
    """Blocking request path; runs inside :func:`asyncio.to_thread`."""
    policy.check(request.url)
    timeout = policy.read_timeout_seconds
    if request.timeout is not None:
        timeout = min(timeout, request.timeout)
    timeout = max(timeout, 0.001)
    host, port = _host_port(request.url)
    connect_timeout = max(min(policy.connect_timeout_seconds, timeout), 0.001)
    try:
        # Bound the connect phase separately from reads: urllib applies one
        # socket timeout to both, so the handshake is probed explicitly.
        with socket.create_connection((host, port), timeout=connect_timeout):
            pass
    except TimeoutError as exc:
        raise TransportTimeout(f"connect to '{host}:{port}' timed out") from exc
    except OSError as exc:
        raise TransportConnectionError(
            f"connect to '{host}:{port}' failed: {type(exc).__name__}"
        ) from exc
    url_request = urlrequest.Request(
        request.url,
        data=request.body or None,
        headers=dict(request.headers),
        method=request.method,
    )
    opener = urlrequest.build_opener(_RedirectDenied)
    try:
        with opener.open(url_request, timeout=timeout) as response:
            return _read_response(response, policy)
    except TransportError:
        raise
    except urlerror.HTTPError as exc:  # non-2xx arrives as an exception
        try:
            return _read_response(exc, policy)
        finally:
            exc.close()
    except urlerror.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TransportTimeout(f"request to '{host}:{port}' timed out") from exc
        raise TransportConnectionError(
            f"request to '{host}:{port}' failed: {type(exc.reason).__name__}"
        ) from exc
    except TimeoutError as exc:
        raise TransportTimeout(f"request to '{host}:{port}' timed out") from exc
    except (http.client.HTTPException, OSError) as exc:
        raise TransportConnectionError(
            f"request to '{host}:{port}' failed: {type(exc).__name__}"
        ) from exc


def urllib_transport(policy: EgressPolicy) -> HttpTransport:
    """Default transport: urllib in a worker thread, guarded by *policy*.

    No retry lives here on purpose — the queue owns retry disposition and a
    hidden retry could duplicate a billed embedding call.
    """

    async def transport(request: HttpRequest) -> HttpResponse:
        return await asyncio.to_thread(_execute, request, policy)

    return transport


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "EgressDenied",
    "EgressPolicy",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "TransportConnectionError",
    "TransportError",
    "TransportRedirect",
    "TransportResponseTooLarge",
    "TransportTimeout",
    "is_restricted_address",
    "urllib_transport",
]
