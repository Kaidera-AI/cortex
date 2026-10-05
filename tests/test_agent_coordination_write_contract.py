"""R98 member coordination fences; public fixtures, no issued keys or stores."""
import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cortex_v2.cli import agent_request as bridge
from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.errors import ClientError, CortexApiError, CortexTransportError
from cortex_v2.clients.transport import HttpResponse
from test_member_client_integration import FixtureReader, member_profile, EXPIRY
from test_client_transport_origin import isolated_transport_environment  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = '12345678-1234-4234-8234-123456789abc'
SCOPE = '23456789-1234-4234-8234-123456789abc'
PREFIX = 'coordination.handoff.'
UNAVAILABLE = 'ERROR: facade request unavailable in this release\n'
WRITES = {
    'create': {'title': 'Public fixture', 'brief': 'Original brief', 'dedup_key': 'stable-fixture'},
    'claim': {'lease_seconds': 900},
    'lease.renew': {'expected_claim_generation': 7, 'lease_seconds': 900},
    'release': {'expected_claim_generation': 7, 'reason': 'Fixture release'},
    'return': {'expected_claim_generation': 7, 'summary': '\u2603' * 5001,
               'work_products': [{'content_id': HANDOFF, 'revision': 3, 'evidence_class': 'test_output'}]},
    'accept': {'note': 'Fixture review'},
    'rework': {'instructions': 'Fixture rework'},
    'retry': {'reason': 'Fixture retry'},
    'fail': {'expected_claim_generation': 7, 'reason': 'Fixture failure'},
    'abandon': {'expected_claim_generation': 7, 'reason': 'Fixture abandon'},
    'withdraw': {'reason': 'Fixture withdrawal'},
}
FENCED = ('lease.renew', 'release', 'return', 'fail', 'abandon')
LEGACY = [
    ('GET', '/handoffs?mine=True&agent=fixture&status=all', ''),
    ('GET', '/handoffs/' + HANDOFF, ''),
    ('POST', '/handoffs', '{"title":"fixture","brief":"original"}'),
    ('POST', '/handoffs/cross-project', '{}'),
    *[('POST', '/handoffs/' + HANDOFF + '/' + action, '{}')
      for action in ('claim', 'complete', 'release', 'return', 'withdraw', 'retarget')],
    ('GET', '/epics', ''), ('GET', '/board', ''), ('GET', '/history', ''),
    ('GET', '/verify/write?kind=handoff&id=' + HANDOFF, ''),
]


def receipt(action='return'):
    return {'state': 'committed', 'operation': PREFIX + action, 'handoff_id': HANDOFF,
            'scope_id': SCOPE, 'status': 'returned', 'claim_generation': 7,
            'revision': 9, 'policy_revision': 2, 'fixture_extra': 'preserved'}


def arguments(action):
    return {'payload': dict(WRITES[action]), 'idempotency_key': 'fixture-explicit-key',
            'path_params': {} if action == 'create' else {'handoff_id': HANDOFF}}


def recording_client():
    reader, calls = FixtureReader(), []
    def transport(*args, **kwargs):
        calls.append((args, kwargs))
        return HttpResponse(200, {'idempotent-replay': 'true'},
                            json.dumps({'data': receipt()}).encode())
    return CortexClient(member_profile('http://127.0.0.1:1', reader), transport=transport), reader, calls


@pytest.mark.parametrize('action', FENCED)
@pytest.mark.parametrize('generation', [None, True, '7', 0, -1, 7.0])
def test_malformed_claim_fence_is_refused_before_member_key(action, generation):
    client, reader, calls = recording_client()
    kwargs = arguments(action)
    if generation is None:
        del kwargs['payload']['expected_claim_generation']
    else:
        kwargs['payload']['expected_claim_generation'] = generation
    with pytest.raises(ClientError):
        client.call(PREFIX + action, **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('action', WRITES)
@pytest.mark.parametrize('key', [None, '', 'x' * 129, 'unissued\r\nmarker', '\x00', '\u2603'])
def test_explicit_safe_idempotency_key_is_required_before_member_key(action, key):
    client, reader, calls = recording_client()
    kwargs = arguments(action)
    kwargs['idempotency_key'] = key
    with pytest.raises(ClientError):
        client.call(PREFIX + action, **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('handoff_id', ['12345678', '../other', HANDOFF + ':accept',
                                      HANDOFF + '?scope=other', HANDOFF + '#fragment'])
def test_native_path_requires_an_exact_uuid_before_member_key(handoff_id):
    client, reader, calls = recording_client()
    kwargs = arguments('return')
    kwargs['path_params']['handoff_id'] = handoff_id
    with pytest.raises(ClientError):
        client.call(PREFIX + 'return', **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('extra', ['claimed_by', 'scope_id', 'agent', 'completed'])
def test_body_cannot_supply_identity_scope_or_fake_completion(extra):
    client, reader, calls = recording_client()
    kwargs = arguments('return')
    kwargs['payload'][extra] = 'unissued-private-marker'
    with pytest.raises(ClientError) as refused:
        client.call(PREFIX + 'return', **kwargs)
    assert 'unissued-private-marker' not in str(refused.value)
    assert reader.reads == 0 and calls == []


@pytest.fixture
def wire():
    servers = []
    def start(*, status=200, body=None, drop=False, replay=False):
        records = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                records.append({'path': self.path, 'body': json.loads(raw),
                                'scope': self.headers.get('X-Cortex-Scope'),
                                'idempotency': self.headers.get('Idempotency-Key')})
                if drop:
                    self.close_connection = True
                    return
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cortex-Key-Expires', EXPIRY)
                self.send_header('X-Request-Id', 'fixture-request')
                self.send_header('Idempotent-Replay', str(replay).lower())
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f'http://127.0.0.1:{server.server_port}', records
    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize('action', WRITES)
def test_valid_native_write_preserves_original_body_key_and_receipt(wire, action):
    expected = receipt(action)
    origin, records = wire(status=201 if action == 'create' else 200,
                           body={'data': expected, 'request_id': 'fixture-body-request',
                                 'contract_version': 'fixture-version'}, replay=True)
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    result = client.call(PREFIX + action, **arguments(action))
    suffix = {'lease.renew': 'renew-lease'}.get(action, action)
    path = '/v1/coordination/handoffs' + ('' if action == 'create' else '/' + HANDOFF + ':' + suffix)
    assert records == [{'path': path, 'body': WRITES[action], 'scope': 'fixture-project',
                        'idempotency': 'fixture-explicit-key'}]
    assert result.data == expected and result.replayed is True and reader.reads == 1
    assert result.request_id == 'fixture-body-request' and result.key_expires_at == EXPIRY
    assert result.contract_version == 'fixture-version'


@pytest.mark.parametrize('field,value', [
    ('state', 'pending'), ('operation', PREFIX + 'accept'), ('handoff_id', SCOPE),
    ('scope_id', 'fixture-project'), ('status', 'completed'),
    ('claim_generation', True), ('claim_generation', -1), ('claim_generation', None),
    ('revision', 0), ('revision', '9'), ('policy_revision', False),
])
def test_invalid_success_receipt_is_an_uncertain_outcome_never_empty_success(wire, field, value):
    data = receipt()
    data[field] = value
    origin, records = wire(body={'data': data})
    reader = FixtureReader()
    client = CortexClient(member_profile(origin, reader))
    with pytest.raises(ClientError) as refused:
        client.call(PREFIX + 'return', **arguments('return'))
    assert 'uncertain' in str(refused.value).lower()
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('body', [{}, {'data': None}, {'data': {}}, {'data': []}])
def test_missing_receipt_never_reports_a_success(wire, body):
    origin, records = wire(body=body)
    reader = FixtureReader()
    with pytest.raises(ClientError):
        CortexClient(member_profile(origin, reader)).call(PREFIX + 'return', **arguments('return'))
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('status,code,retryable', [
    (409, 'stale_claim_generation', False), (409, 'claim_generation_mismatch', False),
    (409, 'lease_expired', False), (403, 'not_current_claimant', False),
    (403, 'scope_write_denied', False), (401, 'credential_revoked', False),
    (409, 'idempotency_key_reused', False), (503, 'outcome_unknown', True),
])
def test_server_write_fence_error_is_preserved_without_retry(wire, status, code, retryable):
    origin, records = wire(status=status, body={'error': {'code': code, 'message': 'Fixture refusal',
                                                         'retryable': retryable},
                                                'request_id': 'fixture-error-request'})
    reader = FixtureReader()
    with pytest.raises(CortexApiError) as refused:
        CortexClient(member_profile(origin, reader)).call(PREFIX + 'return', **arguments('return'))
    error = refused.value
    assert (error.status, error.code, error.retryable) == (status, code, retryable)
    assert error.request_id == 'fixture-error-request' and error.key_expires_at == EXPIRY
    assert records[0]['body']['expected_claim_generation'] == 7
    assert records[0]['idempotency'] == 'fixture-explicit-key'
    assert reader.reads == 1 and len(records) == 1


def test_connection_drop_after_write_never_replays_or_mints_a_new_key(wire):
    origin, records = wire(drop=True)
    reader = FixtureReader()
    with pytest.raises(CortexTransportError):
        CortexClient(member_profile(origin, reader)).call(PREFIX + 'return', **arguments('return'))
    assert records[0]['body'] == WRITES['return']
    assert records[0]['idempotency'] == 'fixture-explicit-key'
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('method,path,body', LEGACY)
def test_unmapped_legacy_coordination_stops_before_profile_key_or_http(monkeypatch, method, path, body):
    def forbidden(*args, **kwargs):
        pytest.fail('unavailable coordination must stop before profile or HTTP')
    monkeypatch.setattr(bridge, 'load_member_profile', forbidden)
    monkeypatch.setattr(bridge, 'http_request', forbidden)
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(['api', method, path], stdin=io.StringIO(body), stdout=out, stderr=err)
    assert (code, out.getvalue(), err.getvalue()) == (2, '', UNAVAILABLE)


@pytest.mark.parametrize('method,path,body', LEGACY)
def test_release_helper_has_same_coordination_unavailable_contract(method, path, body):
    result = subprocess.run(['bash', '-c', 'source "$1"; cortex_api_call "$2" "$3" "$4"',
                             'fixture', str(ROOT / 'scripts/agent-shims/_cortex_api.sh'),
                             method, path, body], capture_output=True, text=True)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', UNAVAILABLE)
