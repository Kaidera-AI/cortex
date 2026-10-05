"""R91 native member integration; no issued keys or credential stores.

Readers use only in-memory unissued markers. Actual loopback receivers retain
credential-version flags, never bearer bytes; every server closes at teardown.
"""
from __future__ import annotations

import importlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex_v2.clients import config
from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.errors import ClientConfigError, CortexApiError
from cortex_v2.clients.key_store import KeyStoreError
from test_client_transport_origin import isolated_transport_environment  # noqa: F401

cli = importlib.import_module('cortex_v2.cli.main')
PROJECT = 'fixture-project'
EXPIRY = '2027-10-05T01:00:00+00:00'


class FixtureReader:
    def __init__(self, **selection):
        self.project = selection.get('project', PROJECT)
        self.selection = selection
        self.reads = 0
        self.version = 1
        self.refusal = None

    def headers(self):
        self.reads += 1
        if self.refusal:
            raise KeyStoreError(self.refusal)
        return {'Authorization': 'Bearer ' + ('A' if self.version == 1 else 'B') * 43,
                'X-Cortex-Scope': self.project}


def member_profile(origin, reader):
    # The baseline already accepts profile objects by attributes. An explicit
    # cached decoy demonstrates that it ignores the selected per-request reader.
    return SimpleNamespace(base_url=origin, token='cached-unissued-decoy',
                           default_scope=PROJECT, default_read_scopes=(),
                           member_reader=reader)


@pytest.fixture
def receiver():
    servers = []

    def create(*, status=200):
        records = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length) if length else b''
                bearer = self.headers.get('Authorization')
                version = (1 if bearer == 'Bearer ' + 'A' * 43 else
                           2 if bearer == 'Bearer ' + 'B' * 43 else 0)
                records.append({'version': version, 'method': self.command,
                                'path': self.path,
                                'scope': self.headers.get('X-Cortex-Scope'),
                                'read_scopes': self.headers.get('X-Cortex-Read-Scopes'),
                                'idempotency': self.headers.get('Idempotency-Key'),
                                'body': json.loads(body) if body else None})
                data = (b'{"data":{"ok":true}}' if status == 200 else
                        b'{"error":{"code":"selected_member_denied","message":"denied","retryable":false}}')
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cortex-Key-Expires', EXPIRY)
                self.end_headers()
                self.wfile.write(data)

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


def test_each_request_reads_once_and_sees_replacement(receiver):
    origin, records = receiver()
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    assert client.call('auth.principal').key_expires_at == EXPIRY
    reader.version = 2
    assert client.call('auth.principal').data == {'ok': True}
    assert reader.reads == 2
    assert [row['version'] for row in records] == [1, 2]
    assert [row['scope'] for row in records] == [PROJECT, PROJECT]


@pytest.mark.parametrize('refusal', ('missing', 'locked', 'expired', 'access denied', 'unsafe'))
def test_reader_refuses_before_transport_even_after_a_success(receiver, refusal):
    origin, records = receiver()
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    client.call('auth.principal')
    reader.refusal = refusal
    with pytest.raises(ClientConfigError):
        client.call('auth.principal')
    assert reader.reads == 2
    assert len(records) == 1


@pytest.mark.parametrize('status', (401, 403))
def test_remote_denial_is_not_retried_or_replaced(receiver, status):
    origin, records = receiver(status=status)
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    with pytest.raises(CortexApiError) as denied:
        client.call('auth.principal')
    assert denied.value.status == status
    assert denied.value.code == 'selected_member_denied'
    assert denied.value.key_expires_at == EXPIRY
    assert reader.reads == 1
    assert len(records) == 1 and records[0]['version'] == 1


@pytest.mark.parametrize('selection', ({'scope': 'other'}, {'read_scopes': ('other',)},
                                       {'read_scopes': (PROJECT, 'other')}))
def test_member_cannot_widen_write_or_read_scope(receiver, selection):
    origin, records = receiver()
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    with pytest.raises(ClientConfigError):
        client.call('capability.discover', **selection)
    assert reader.reads == 0
    assert records == []


def test_member_preserves_body_query_idempotency_scope_and_timeout(receiver):
    origin, records = receiver()
    reader = FixtureReader()
    from cortex_v2.clients.transport import http_request
    timeouts = []

    def transport(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        return http_request(*args, **kwargs)

    client = CortexClient(member_profile(origin, reader), transport=transport, timeout=2.25)
    client.call('memory.record', payload={'body': 'snowman \u2603', 'record_type': 'note'},
                query={'mode': 'exact'}, scope=PROJECT, idempotency_key='fixture-idempotency')
    client.call('capability.discover', scope=PROJECT, read_scopes=(PROJECT,),
                query={'filter': 'a b'})
    assert reader.reads == 2 and timeouts == [2.25, 2.25]
    assert records[0] == {'version': 1, 'method': 'POST',
                          'path': '/v1/memory/records?mode=exact', 'scope': PROJECT,
                          'read_scopes': None, 'idempotency': 'fixture-idempotency',
                          'body': {'body': 'snowman \u2603', 'record_type': 'note'}}
    assert records[1]['path'] == '/v1/capabilities?filter=a+b'
    assert records[1]['read_scopes'] == PROJECT


def load_member(*args, **kwargs):
    loader = getattr(config, 'load_member_profile', None)
    assert callable(loader), 'native member profile loader is missing'
    return loader(*args, **kwargs)


def selection(tmp_path):
    return {'installation': 'fixture-installation', 'project': PROJECT,
            'name': 'fixture-tool', 'project_root': tmp_path}


def test_member_profile_retains_reader_not_bearer_and_performs_no_key_read(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'MemberKeyReader', FixtureReader, raising=False)
    profile = load_member(env={'CORTEX_URL': 'http://127.0.0.1:1'}, **selection(tmp_path))
    assert profile.token == ''
    assert profile.default_scope == PROJECT and profile.default_read_scopes == ()
    assert profile.member_reader.reads == 0
    assert profile.member_reader.selection == selection(tmp_path)
    assert 'member_reader=' not in repr(profile)


@pytest.mark.parametrize('missing', ('installation', 'project', 'name', 'project_root'))
def test_every_member_selector_is_required(tmp_path, missing):
    chosen = selection(tmp_path)
    chosen.pop(missing)
    with pytest.raises(ClientConfigError):
        load_member(env={'CORTEX_URL': 'http://127.0.0.1:1'}, **chosen)


@pytest.mark.parametrize('name', ('owner', 'lead', 'recovery', 'OWNER'))
def test_member_loader_refuses_reserved_privileged_identity(tmp_path, name):
    chosen = selection(tmp_path)
    chosen['name'] = name
    with pytest.raises(ClientConfigError):
        load_member(env={'CORTEX_URL': 'http://127.0.0.1:1'}, **chosen)
    assert not (tmp_path / '.kaidera').exists()


@pytest.mark.parametrize('ambient', ({'CI': 'true', 'CORTEX_KEY': 'unissued-ci-decoy'},
                                    {'CORTEX_V2_TOKEN': 'unissued-legacy-decoy'}))
def test_member_loader_never_uses_ambient_ci_or_legacy_key(tmp_path, ambient):
    with pytest.raises(ClientConfigError):
        load_member(env={'CORTEX_URL': 'http://127.0.0.1:1', **ambient}, **selection(tmp_path))
    assert not (tmp_path / '.kaidera').exists()


@pytest.mark.parametrize('denied', (False, True))
def test_actual_cli_loads_explicit_member_without_owner_and_maps_refusal(monkeypatch, tmp_path, receiver, denied):
    origin, records = receiver()
    readers = []

    def make_reader(**chosen):
        reader = FixtureReader(**chosen)
        reader.refusal = 'locked' if denied else None
        readers.append(reader)
        return reader

    def owner_forbidden(*args, **kwargs):
        raise AssertionError('generic member CLI consulted the owner store')

    monkeypatch.setattr(config, 'MemberKeyReader', make_reader, raising=False)
    monkeypatch.setattr(config, 'KeyStore', owner_forbidden)
    for name in (*config.LEGACY_CREDENTIAL_ENV, 'CORTEX_KEY', 'CORTEX_URL'):
        monkeypatch.delenv(name, raising=False)
    connection = tmp_path / 'connection.json'
    connection.write_text(json.dumps({'profile': 'v2', 'base_url': origin,
                                     **{key: str(value) for key, value in selection(tmp_path).items()}}))
    connection.chmod(0o600)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(['--config', str(connection), 'auth.principal'], out=out, err=err)
    assert code == (cli.EXIT_USAGE if denied else cli.EXIT_OK)
    assert len(readers) == 1 and readers[0].reads == 1
    assert len(records) == (0 if denied else 1)
    assert 'Bearer' not in out.getvalue() + err.getvalue()
    if denied:
        assert out.getvalue() == '' and 'configuration error' in err.getvalue()
    else:
        assert json.loads(out.getvalue())['data'] == {'ok': True}
        assert records[0]['version'] == 1
