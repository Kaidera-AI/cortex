"""R63: one selected origin, no redirect replay or inherited proxy routing.

Only independent ephemeral loopback receivers and an unissued in-memory bearer
marker are used. Receiver records never retain or print credential bytes.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cortex_v2.clients.transport import http_request


@pytest.fixture(autouse=True)
def isolated_transport_environment(monkeypatch):
    for name in (
        'http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY',
        'all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY', 'REQUEST_METHOD',
    ):
        monkeypatch.delenv(name, raising=False)
    # The baseline urlopen caches its process-global opener. Each scenario must
    # derive its own environment so the proxy RED cannot be masked by the cache.
    monkeypatch.setattr(urllib.request, '_opener', None)


@pytest.fixture
def receiver():
    servers = []

    def create(*, status=200, location=None, body=b'{"ok":true}', headers=None):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self):
                length = int(self.headers.get('Content-Length', '0'))
                data = self.rfile.read(length) if length else b''
                records.append({
                    'method': self.command,
                    'path': self.path,
                    'has_bearer': self.headers.get('Authorization') is not None,
                    'scope': self.headers.get('X-Cortex-Scope'),
                    'body': json.loads(data) if data else None,
                })
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if location:
                    self.send_header('Location', location)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            do_GET = respond
            do_POST = respond

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}', records

    yield create
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def member_headers():
    return {'Authorization': 'Bearer synthetic-unissued-marker',
            'X-Cortex-Scope': 'fixture-project'}


@pytest.mark.parametrize('status', (301, 302, 303, 307, 308))
def test_get_redirect_does_not_contact_another_origin(receiver, status):
    target, target_requests = receiver()
    origin, origin_requests = receiver(status=status, location=target + '/sink')
    response = http_request('GET', origin + '/projects',
                            headers=member_headers(), timeout=2)
    assert len(origin_requests) == 1
    assert target_requests == []
    assert response.status == status
    assert response.headers['location'] == target + '/sink'


@pytest.mark.parametrize('status', (301, 302, 303))
def test_post_redirect_never_replays_as_get(receiver, status):
    target, target_requests = receiver()
    origin, origin_requests = receiver(status=status, location=target + '/sink')
    response = http_request('POST', origin + '/effects',
                            headers=member_headers(),
                            json_body={'effect': 'synthetic'}, timeout=2)
    assert len(origin_requests) == 1
    assert origin_requests[0]['method'] == 'POST'
    assert target_requests == []
    assert response.status == status


def test_inherited_proxy_receives_no_member_request(receiver, monkeypatch):
    proxy, proxy_requests = receiver()
    origin, origin_requests = receiver()
    monkeypatch.setenv('http_proxy', proxy)
    # Make the environment route observable even on hosts that normally bypass
    # loopback proxies. The replacement transport must not consult this policy.
    monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda _: False)
    response = http_request('GET', origin + '/projects',
                            headers=member_headers(), timeout=2)
    assert proxy_requests == []
    assert len(origin_requests) == 1
    assert origin_requests[0]['has_bearer'] is True
    assert response.status == 200


def test_selected_origin_retains_query_body_and_scope(receiver):
    origin, requests = receiver(body=b'{"exact":"result"}')
    response = http_request('POST', origin + '/effects',
                            headers=member_headers(),
                            json_body={'text': 'caf\u00e9'},
                            query={'filter': 'a b', 'limit': '2'}, timeout=2)
    assert requests == [{'method': 'POST',
                         'path': '/effects?filter=a+b&limit=2',
                         'has_bearer': True, 'scope': 'fixture-project',
                         'body': {'text': 'caf\u00e9'}}]
    assert response.status == 200
    assert response.body == b'{"exact":"result"}'


@pytest.mark.parametrize('status', (401, 403, 503))
def test_error_status_body_and_expiry_survive_without_retry(receiver, status):
    body = b'{"error":{"code":"fixture_failure"}}'
    expiry = '2027-10-05T00:00:00Z'
    origin, requests = receiver(status=status, body=body,
                                headers={'Cortex-Key-Expires': expiry})
    response = http_request('GET', origin + '/projects',
                            headers=member_headers(), timeout=2)
    assert len(requests) == 1
    assert response.status == status
    assert response.body == body
    assert response.headers['cortex-key-expires'] == expiry
