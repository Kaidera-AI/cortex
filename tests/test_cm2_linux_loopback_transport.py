"""Concrete private loopback HTTP effects, no live Cortex or engine."""
from contextlib import contextmanager
import importlib
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


def product():
    module = importlib.import_module('cortex_v2.clients.linux_provisioning')
    assert hasattr(module, 'LoopbackTransport'), 'accepted concrete loopback transport is absent'
    return module


def headers():
    return {'Authorization': 'Bearer ' + secrets.token_urlsafe(32), 'X-Cortex-Scope': 'fresh'}


@contextmanager
def endpoint(routes):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            status, body, extra, delay = routes.get(self.path, (404, b'', {}, 0))
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            for key, value in extra.items(): self.send_header(key, value)
            self.end_headers()
            try:
                if delay:
                    self.wfile.write(body[:1]); self.wfile.flush(); time.sleep(delay)
                    self.wfile.write(body[1:])
                else: self.wfile.write(body)
            except OSError: pass
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def handle_error(self, *args): pass
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try: yield 'http://127.0.0.1:' + str(server.server_address[1]), calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)


def test_actual_loopback_get_uses_explicit_private_headers_and_ignores_proxy_env(monkeypatch):
    module = product(); private = headers()
    for key in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.setenv(key, 'http://127.0.0.1:1')
    with endpoint({'/health/ready': (200, b'{"status":"ready"}', {}, 0)}) as (origin, calls):
        value = module.LoopbackTransport(origin).get(origin + '/health/ready', headers=private, timeout=1, max_bytes=65536)
        assert value == (200, b'{"status":"ready"}') and len(calls) == 1
        assert bool(calls[0][1]['Authorization'] == private['Authorization'])
        assert calls[0][1]['X-Cortex-Scope'] == 'fresh'


def test_actual_redirect_never_replays_private_headers_to_another_origin():
    module = product(); private = headers()
    with endpoint({'/foreign': (200, b'foreign', {}, 0)}) as (foreign, foreign_calls):
        with endpoint({'/health/ready': (302, b'', {'Location': foreign + '/foreign'}, 0)}) as (origin, calls):
            status, raw = module.LoopbackTransport(origin).get(origin + '/health/ready', headers=private, timeout=1, max_bytes=65536)
            assert status == 302 and raw == b'' and len(calls) == 1 and foreign_calls == []


@pytest.mark.parametrize('body,limit', [(b'123456789', 8), (b'x' * 65537, 65536)])
def test_actual_response_overflow_refuses_without_returning_private_body(body, limit):
    module = product(); private = headers()
    with endpoint({'/health/ready': (200, body, {}, 0)}) as (origin, calls):
        with pytest.raises(module.ProvisionRefusal) as error:
            module.LoopbackTransport(origin).get(origin + '/health/ready', headers=private, timeout=1, max_bytes=limit)
        assert error.value.code == 'cortex_health_unavailable'
        assert bool(private['Authorization'] not in str(error.value)) and len(calls) == 1


def test_actual_partial_response_has_one_total_deadline():
    module = product(); private = headers()
    with endpoint({'/health/ready': (200, b'{"status":"ready"}', {}, .45)}) as (origin, calls):
        start = time.monotonic()
        with pytest.raises(module.ProvisionRefusal) as error:
            module.LoopbackTransport(origin).get(origin + '/health/ready', headers=private, timeout=.1, max_bytes=65536)
        assert time.monotonic() - start < .4
        assert error.value.code == 'cortex_health_unavailable' and len(calls) == 1


@pytest.mark.parametrize('origin', ['https://127.0.0.1:18612', 'http://localhost:18612',
    'http://[::1]:18612', 'http://user:private@127.0.0.1:18612', 'http://127.0.0.1',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:18612/path',
    'http://127.0.0.1:18612?query', 'http://127.0.0.1:18612#fragment',
    'http://127.0.0.1.example:18612'])
def test_invalid_origin_refuses_before_any_socket_effect(origin, monkeypatch):
    module = product(); effects = []
    monkeypatch.setattr(module.http.client, 'HTTPConnection', lambda *a, **kw: effects.append(True))
    with pytest.raises(module.ProvisionRefusal): module.LoopbackTransport(origin)
    assert effects == []


@pytest.mark.parametrize('change', ['foreign-origin', 'userinfo', 'fragment', 'wrong-token',
    'header-injection', 'extra-header', 'missing-scope', 'oversize', 'zero-timeout', 'boolean-timeout'])
def test_invalid_request_refuses_before_any_private_http_request(change):
    module = product(); private = headers()
    with endpoint({}) as (origin, calls):
        url = origin + '/health/ready'; timeout = 1; maximum = 65536
        if change == 'foreign-origin': url = 'http://127.0.0.1:1/health/ready'
        elif change == 'userinfo': url = origin.replace('http://', 'http://user:private@') + '/health/ready'
        elif change == 'fragment': url += '#fragment'
        elif change == 'wrong-token': private['Authorization'] = 'Bearer invalid'
        elif change == 'header-injection': private['X-Cortex-Scope'] = 'fresh\r\nInjected: prohibited'
        elif change == 'extra-header': private['Private'] = 'prohibited'
        elif change == 'missing-scope': private.pop('X-Cortex-Scope')
        elif change == 'oversize': maximum = 65537
        elif change == 'zero-timeout': timeout = 0
        elif change == 'boolean-timeout': timeout = True
        with pytest.raises(module.ProvisionRefusal):
            module.LoopbackTransport(origin).get(url, headers=private, timeout=timeout, max_bytes=maximum)
        assert calls == []


def test_actual_transport_drives_existing_member_health_principal_and_roster_admission(tmp_path):
    module = product(); custody = importlib.import_module('cortex_v2.clients.native_prerequisite')
    home = Path(__file__).parent / 'fixtures/cm2-revision4/fixtures'
    connection = json.loads((home / 'connection.linux.json').read_text())
    principal = json.loads((home / 'principal.linux.json').read_text())
    roster = json.loads((home / 'roster.linux.json').read_text())
    connection['project_root'] = str(tmp_path)
    private = headers(); private['X-Cortex-Scope'] = connection['project']
    routes = {'/health/ready': (200, b'{"status":"ready"}', {}, 0),
              '/v1/auth/principal': (200, json.dumps({'data': principal}).encode(), {}, 0),
              '/v1/scopes/' + connection['project'] + '/roster': (200, json.dumps({'data': roster}).encode(), {}, 0)}
    with endpoint(routes) as (origin, calls):
        connection['origin'] = origin
        value = custody.read_member_admission(connection, reader=SimpleNamespace(headers=lambda: dict(private)),
                                              transport=module.LoopbackTransport(origin))
        assert value['principal_id'] == connection['principal_id'] and value['actor_id'] == connection['actor_id']
        assert value['role'] == 'member' and value['can_publish'] is False and len(calls) == 3
        assert all(bool(c[1]['Authorization'] == private['Authorization']) for c in calls)
