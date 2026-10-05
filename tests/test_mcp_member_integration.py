"""R91 actual stdio member calls; local reader context is never a wire grant."""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cortex_v2.clients import config
from cortex_v2.clients.client import CallResult
from cortex_v2.mcp.server import create_mcp_app, run_stdio
from test_member_client_integration import (
    EXPIRY, PROJECT, FixtureReader, receiver, isolated_transport_environment,  # noqa: F401
)


def tool(name='auth_principal', **arguments):
    return {'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}}


def setup_member(monkeypatch, tmp_path, origin):
    reader = FixtureReader()
    old_store_reads = []

    def selected_reader(**chosen):
        reader.selection = chosen
        assert chosen == {'installation': 'fixture-installation', 'project': PROJECT,
                          'name': 'fixture-tool', 'project_root': tmp_path}
        return reader

    class OldStore:
        def __init__(self, installation):
            self.installation = installation

        def get(self, project, name):
            old_store_reads.append({'project': project, 'name': name})
            return 'A' * 43  # explicitly unissued, in memory only

    monkeypatch.setattr(config, 'MemberKeyReader', selected_reader)
    monkeypatch.setattr(config, 'KeyStore', OldStore)
    for name in (*config.LEGACY_CREDENTIAL_ENV, 'CORTEX_KEY', 'CORTEX_URL'):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / 'connection.json'
    path.write_text(json.dumps({'profile': 'v2', 'base_url': origin,
                               'installation': 'fixture-installation', 'project': PROJECT,
                               'name': 'fixture-tool', 'project_root': str(tmp_path)}))
    path.chmod(0o600)
    return reader, old_store_reads, path


def session(monkeypatch, path, messages, before=None):
    class Input:
        def __iter__(self):
            for index, message in enumerate(messages):
                if before:
                    before(index)
                yield json.dumps(message) + '\n'

    output = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', Input())
    monkeypatch.setattr(sys, 'stdout', output)
    run_stdio(config=str(path))
    data = output.getvalue()
    assert 'Bearer' not in data and 'A' * 43 not in data and 'B' * 43 not in data
    return [json.loads(line) for line in data.splitlines()]


def test_stdio_initialization_and_listing_never_read_a_key(monkeypatch, tmp_path, receiver):
    origin, requests = receiver()
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)
    responses = session(monkeypatch, path, [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    ])
    assert len(responses) == 2
    assert reader.reads == 0 and old == [] and requests == []


def test_stdio_reads_replacement_once_for_each_actual_request(monkeypatch, tmp_path, receiver):
    origin, requests = receiver()
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)

    def before(index):
        if index == 1:
            reader.version = 2

    responses = session(monkeypatch, path, [tool(), tool()], before)
    assert all(not row['result']['isError'] for row in responses)
    assert reader.reads == 2 and old == []
    assert [row['version'] for row in requests] == [1, 2]
    assert [row['scope'] for row in requests] == [PROJECT, PROJECT]


@pytest.mark.parametrize('refusal', ('missing', 'locked', 'expired', 'access denied', 'unsafe'))
def test_stdio_refusal_after_success_never_reuses_the_prior_key(monkeypatch, tmp_path, receiver, refusal):
    origin, requests = receiver()
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)

    def before(index):
        if index == 1:
            reader.refusal = refusal

    responses = session(monkeypatch, path, [tool(), tool()], before)
    assert not responses[0]['result']['isError']
    assert responses[1]['result']['isError']
    failure = json.loads(responses[1]['result']['content'][0]['text'])['error']
    assert failure['code'] == 'ClientConfigError'
    assert reader.reads == 2 and old == [] and len(requests) == 1


@pytest.mark.parametrize('status', (401, 403))
def test_stdio_remote_denial_preserves_error_and_expiry_with_no_replay(monkeypatch, tmp_path, receiver, status):
    origin, requests = receiver(status=status)
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)
    response = session(monkeypatch, path, [tool()])[0]
    failure = json.loads(response['result']['content'][0]['text'])['error']
    assert response['result']['isError']
    assert failure['code'] == 'selected_member_denied' and failure['status'] == status
    assert failure['key_expires_at'] == EXPIRY
    assert reader.reads == 1 and old == [] and len(requests) == 1


@pytest.mark.parametrize('request', (tool(scope='other'),
                                    tool('capability_discover', read_scopes=['other']),
                                    tool('memory_record', body='fixture'),
                                    tool('unknown_fixture_tool')))
def test_stdio_invalid_operation_scope_or_idempotency_never_reads_key(monkeypatch, tmp_path, receiver, request):
    origin, requests = receiver()
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)
    response = session(monkeypatch, path, [request])[0]
    assert response.get('error') or response.get('result', {}).get('isError')
    assert reader.reads == 0 and old == [] and requests == []


def test_stdio_preserves_actual_write_payload_and_idempotency(monkeypatch, tmp_path, receiver):
    origin, requests = receiver()
    reader, old, path = setup_member(monkeypatch, tmp_path, origin)
    response = session(monkeypatch, path, [tool('memory_record', body='fixture \u2603',
                                               record_type='note', idempotency_key='fixture-mcp')])[0]
    result = json.loads(response['result']['content'][0]['text'])
    assert not response['result']['isError'] and result['data'] == {'ok': True}
    assert result['key_expires_at'] == EXPIRY
    assert reader.reads == 1 and old == []
    assert requests == [{'version': 1, 'method': 'POST', 'path': '/v1/memory/records',
                         'scope': PROJECT, 'read_scopes': None, 'idempotency': 'fixture-mcp',
                         'body': {'body': 'fixture \u2603', 'record_type': 'note'}}]


def test_http_cannot_claim_local_member_context_without_a_bearer():
    calls = []

    def factory(credentials):
        calls.append(credentials)
        raise AssertionError('unauthenticated HTTP request reached the factory')

    with TestClient(create_mcp_app(client_factory=factory)) as client:
        request = tool(member_reader=True, local_member=True, token='unissued-json-decoy')
        request['member_reader'] = {'project': PROJECT}
        response = client.post('/mcp', json=request,
                               headers={'X-Cortex-Member-Reader': 'true', 'X-Cortex-Scope': PROJECT})
    assert response.status_code == 401 and calls == []


def test_http_bearer_still_has_no_local_member_reader():
    observations = []

    class Client:
        def call(self, operation_id, **kwargs):
            return CallResult(operation_id, 200, {'ok': True}, False, 'fixture-request')

    def factory(credentials):
        observations.append((credentials.token == 'unissued-http-marker',
                             credentials.member_reader is None, credentials.scope))
        return Client()

    with TestClient(create_mcp_app(client_factory=factory)) as client:
        response = client.post('/mcp', json=tool(),
                               headers={'Authorization': 'Bearer unissued-http-marker',
                                        'X-Cortex-Scope': PROJECT})
    assert response.status_code == 200 and observations == [(True, True, PROJECT)]


def test_mcp_entrypoint_passes_physical_root_only_to_stdio(monkeypatch, tmp_path):
    entry = importlib.import_module('cortex_v2.mcp.__main__')
    server = importlib.import_module('cortex_v2.mcp.server')
    calls = []
    monkeypatch.setattr(server, 'run_stdio', lambda **kwargs: calls.append(kwargs))
    assert entry.main(['--stdio', '--project-root', str(tmp_path)]) == 0
    assert calls[0]['project_root'] == tmp_path
    with pytest.raises(SystemExit) as refused:
        entry.main(['--http', '--project-root', str(tmp_path)])
    assert refused.value.code == 2 and len(calls) == 1
