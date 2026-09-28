"""Provider transport unit tests: egress policy, SSRF guard and the default
urllib transport.

Pure unit tests except the real-socket cases, which bind a
``ThreadingHTTPServer`` on 127.0.0.1 only: no external network, no database.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading
import time

import pytest

from cortex_v2.processing import providers
from cortex_v2.processing.providers import (
    EgressDenied,
    EgressPolicy,
    HttpRequest,
    TransportConnectionError,
    TransportRedirect,
    TransportResponseTooLarge,
    TransportTimeout,
    is_restricted_address,
    urllib_transport,
)


def run(coroutine):
    return asyncio.run(coroutine)


def policy(**overrides):
    base = dict(
        allowlist=("api.test",),
        allow_private_for=(),
        connect_timeout_seconds=2.0,
        read_timeout_seconds=5.0,
        max_response_bytes=65_536,
    )
    base.update(overrides)
    return EgressPolicy(**base)


def local_policy(**overrides):
    base = dict(
        allowlist=("127.0.0.1",),
        allow_private_for=("127.0.0.1",),
        connect_timeout_seconds=2.0,
        read_timeout_seconds=5.0,
        max_response_bytes=65_536,
    )
    base.update(overrides)
    return EgressPolicy(**base)


# ---------------------------------------------------------------------------
# Egress policy / SSRF guard
# ---------------------------------------------------------------------------


def test_check_allows_allowlisted_public_https_host():
    # Numeric public host: getaddrinfo needs no DNS, so this is offline-safe.
    EgressPolicy(allowlist=("93.184.216.34",)).check("https://93.184.216.34/embeddings")


def test_check_denies_host_outside_allowlist_before_resolution(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("getaddrinfo must not run for a non-allowlisted host")

    monkeypatch.setattr(providers.socket, "getaddrinfo", explode)
    with pytest.raises(EgressDenied, match="allowlist"):
        policy().check("https://other.test/embeddings")


def test_check_denies_http_metadata_endpoint():
    with pytest.raises(EgressDenied, match="scheme") as excinfo:
        EgressPolicy(allowlist=("169.254.169.254",)).check(
            "http://169.254.169.254/latest/meta-data"
        )
    assert "169.254.169.254" in excinfo.value.reason


def test_check_denies_link_local_address_even_when_allowlisted():
    with pytest.raises(EgressDenied, match="restricted"):
        EgressPolicy(allowlist=("169.254.169.254",)).check(
            "https://169.254.169.254/latest/meta-data"
        )


def test_check_denies_http_for_localhost_without_private_allowance():
    with pytest.raises(EgressDenied, match="scheme"):
        EgressPolicy(allowlist=("localhost",)).check("http://localhost/")


def test_check_denies_loopback_resolution_for_localhost():
    with pytest.raises(EgressDenied, match="restricted"):
        EgressPolicy(allowlist=("localhost",)).check("https://localhost/")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "10.1.2.3", "192.168.0.1", "224.0.0.1", "240.0.0.1", "0.0.0.0"],
)
def test_check_denies_restricted_numeric_hosts(host):
    with pytest.raises(EgressDenied, match="restricted"):
        EgressPolicy(allowlist=(host,)).check(f"https://{host}/")


def test_check_denies_ipv6_loopback():
    with pytest.raises(EgressDenied, match="restricted"):
        EgressPolicy(allowlist=("::1",)).check("https://[::1]/")


def test_check_denies_ipv4_mapped_loopback():
    with pytest.raises(EgressDenied, match="restricted"):
        mapped = EgressPolicy(allowlist=("::ffff:127.0.0.1",))
        mapped.check("https://[::ffff:127.0.0.1]/")


def test_check_allows_private_host_listed_in_allow_private_for():
    EgressPolicy(allowlist=("127.0.0.1",), allow_private_for=("127.0.0.1",)).check(
        "http://127.0.0.1:11434/api/embed"
    )


def test_check_denies_non_http_schemes_even_for_privileged_hosts():
    with pytest.raises(EgressDenied, match="scheme"):
        EgressPolicy(allowlist=("api.test",), allow_private_for=("api.test",)).check(
            "ftp://api.test/x"
        )


def test_check_denies_http_scheme_for_remote_host():
    with pytest.raises(EgressDenied, match="scheme"):
        policy().check("http://api.test/embeddings")


@pytest.mark.parametrize("url", ["https://", "not-a-url", "https://:8443/x"])
def test_check_denies_urls_without_host(url):
    with pytest.raises(EgressDenied):
        policy(allowlist=("api.test", "not-a-url")).check(url)


def test_allow_private_for_does_not_bypass_allowlist():
    with pytest.raises(EgressDenied, match="allowlist"):
        EgressPolicy(allowlist=(), allow_private_for=("127.0.0.1",)).check(
            "http://127.0.0.1/"
        )


def test_policy_normalizes_hosts_to_lowercase():
    built = EgressPolicy(allowlist=("API.Test",), allow_private_for=("LOCALHOST",))
    assert built.allowlist == ("api.test",)
    assert built.allow_private_for == ("localhost",)


def test_is_restricted_address_classification():
    import ipaddress

    assert is_restricted_address(ipaddress.ip_address("127.0.0.1"))
    assert is_restricted_address(ipaddress.ip_address("169.254.169.254"))
    assert is_restricted_address(ipaddress.ip_address("::1"))
    assert not is_restricted_address(ipaddress.ip_address("93.184.216.34"))


def test_transport_denies_ssrf_targets_before_any_request(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no socket may be opened for a denied url")

    monkeypatch.setattr(providers.socket, "create_connection", explode)
    transport = urllib_transport(
        EgressPolicy(
            allowlist=("api.test", "169.254.169.254", "localhost"),
            connect_timeout_seconds=2.0,
            read_timeout_seconds=5.0,
        )
    )
    denied = (
        "https://other.test/embeddings",  # not allowlisted
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://localhost/",  # loopback without allow_private_for
        "http://api.test/embeddings",  # http scheme for a remote host
    )
    for url in denied:
        with pytest.raises(EgressDenied):
            run(transport(HttpRequest("POST", url, body=b"{}")))


# ---------------------------------------------------------------------------
# Default urllib transport against a real local socket
# ---------------------------------------------------------------------------


def serve(respond):
    """Run a throwaway HTTP server on 127.0.0.1; ``respond(handler, body)``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            respond(self, self.rfile.read(length))

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_urllib_transport_round_trips_against_real_socket():
    seen = {}

    def respond(handler, body):
        seen["path"] = handler.path
        seen["auth"] = handler.headers.get("Authorization")
        payload = json.dumps({"echo": json.loads(body)}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("X-Probe", "hit")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, thread, port = serve(respond)
    try:
        transport = urllib_transport(local_policy())
        request = HttpRequest(
            "POST",
            f"http://127.0.0.1:{port}/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer probe-token",
            },
            body=b'{"model":"m","input":["x"]}',
        )
        response = run(transport(request))
        assert response.status == 200
        assert response.headers["x-probe"] == "hit"  # keys normalized lowercase
        assert response.headers["content-type"] == "application/json"
        assert json.loads(response.body) == {"echo": {"model": "m", "input": ["x"]}}
        assert seen == {"path": "/embeddings", "auth": "Bearer probe-token"}
    finally:
        stop(server, thread)


def test_urllib_transport_returns_error_status_as_response():
    def respond(handler, body):
        payload = b'{"error":"boom"}'
        handler.send_response(503)
        handler.send_header("Retry-After", "7")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, thread, port = serve(respond)
    try:
        transport = urllib_transport(local_policy())
        request = HttpRequest("POST", f"http://127.0.0.1:{port}/x", body=b"{}")
        response = run(transport(request))
        assert response.status == 503
        assert response.headers["retry-after"] == "7"
        assert response.body == b'{"error":"boom"}'
    finally:
        stop(server, thread)


def test_urllib_transport_refuses_redirects():
    def respond(handler, body):
        handler.send_response(302)
        handler.send_header("Location", "http://127.0.0.1:1/elsewhere")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    server, thread, port = serve(respond)
    try:
        transport = urllib_transport(local_policy())
        with pytest.raises(TransportRedirect):
            request = HttpRequest("POST", f"http://127.0.0.1:{port}/x", body=b"{}")
            run(transport(request))
    finally:
        stop(server, thread)


def test_urllib_transport_caps_response_bytes():
    def respond(handler, body):
        payload = b"x" * 5_000
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, thread, port = serve(respond)
    try:
        transport = urllib_transport(local_policy(max_response_bytes=100))
        with pytest.raises(TransportResponseTooLarge):
            run(transport(HttpRequest("GET", f"http://127.0.0.1:{port}/x")))
    finally:
        stop(server, thread)


def test_urllib_transport_maps_read_timeout():
    def respond(handler, body):
        time.sleep(1.2)
        try:
            handler.send_response(200)
            handler.send_header("Content-Length", "2")
            handler.end_headers()
            handler.wfile.write(b"ok")
        except OSError:
            pass

    server, thread, port = serve(respond)
    try:
        transport = urllib_transport(local_policy(read_timeout_seconds=0.3))
        with pytest.raises(TransportTimeout):
            request = HttpRequest("POST", f"http://127.0.0.1:{port}/x", body=b"{}")
            run(transport(request))
    finally:
        stop(server, thread)


def test_urllib_transport_reports_connection_refused():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    transport = urllib_transport(local_policy())
    with pytest.raises(TransportConnectionError):
        run(transport(HttpRequest("GET", f"http://127.0.0.1:{dead_port}/x")))
