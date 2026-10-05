"""R105 native member preservation and explicit unavailable legacy surfaces."""
import io
import json
import subprocess
from pathlib import Path

import pytest

from cortex_v2.cli import agent_request as bridge
from cortex_v2.clients.client import CortexClient
from cortex_v2.clients.errors import ClientError, CortexApiError
from cortex_v2.interface.registry import build_registry
from test_agent_coordination_write_contract import wire, recording_client  # noqa: F401
from test_member_client_integration import FixtureReader, member_profile, receiver, EXPIRY  # noqa: F401
from test_client_transport_origin import isolated_transport_environment  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
ID = '12345678-1234-4234-8234-123456789abc'
PROJECT = 'fixture-project'
TEXT = 'Original \u2603\n' * 501
TRANSCRIPT = json.dumps({'role': 'user', 'text': TEXT}, ensure_ascii=False) + '\n'
SOURCE = {'connector_namespace': 'public-fixture', 'source_key': 'original-source'}
REPO = {'repository_key': 'public.fixture'}
NATIVE = {
    'memory.search': ('/v1/memory/searches', {'query': '\u2603 original search', 'intent': 'concept',
                                          'read_scopes': [PROJECT], 'limit': 25}),
    'memory.inspect-hit': ('/v1/memory/hits:inspect', {'citation': {'content_id': ID, 'revision': 3,
                                                               'span_start': 0, 'span_end': 9}}),
    'graph.explore': ('/v1/graphs:explore', {'kind': 'memory', 'content_id': ID, 'read_scopes': [PROJECT]}),
    'graph.extract': ('/v1/graphs/memory:extract', {'content_id': ID, 'revision': 3}),
    'graph.retract-source': ('/v1/graphs/memory:retract-source', {'content_id': ID, 'reason': 'source_invalidated'}),
    'memory.rebuild-projections': ('/v1/memory/projections:rebuild', {}),
    'code.publish-index': ('/v1/code/index:publish', dict(REPO, commit_sha='a' * 40,
                            files=[{'path': 'src/public.py', 'source': 'def public():\n    return 1\n'}])),
    'code.annotate': ('/v1/code/annotations', dict(REPO, symbol_key='public', annotation='Original authored annotation')),
    'code.callers': ('/v1/code/callers', dict(REPO, symbol_key='public', read_scopes=[PROJECT])),
    'code.impact': ('/v1/code/impact', dict(REPO, changed_files=['src/public.py'], read_scopes=[PROJECT])),
    'code.blast-radius': ('/v1/code/blast-radius', dict(REPO, changed_symbols=['public'], read_scopes=[PROJECT])),
    'code.hotspots': ('/v1/code/hotspots', dict(REPO, head_commit='b' * 40, read_scopes=[PROJECT])),
    'code.assess-change': ('/v1/code/assess-change', dict(REPO, base_commit='a' * 40,
                                 changed_files=['src/public.py'], read_scopes=[PROJECT])),
    'ingest.session': ('/v1/ingest/sessions', dict(SOURCE, transcript_format='generic_jsonl',
                              transcript=TRANSCRIPT, source_observed_at='2026-10-05T10:00:00Z')),
    'ingest.message': ('/v1/ingest/messages', dict(SOURCE, transcript_format='generic_jsonl', transcript=TRANSCRIPT)),
    'ingest.local_state': ('/v1/ingest/local-state', dict(SOURCE, capture={'original': TEXT, 'nested': [1, True]},
                                                       captured_at='2026-10-05T10:00:00Z')),
    'ingest.diary': ('/v1/ingest/diaries', dict(SOURCE, entries=[{'entry_date': '2026-10-05', 'text': TEXT}])),
    'ingest.save_chat': ('/v1/ingest/save-chats', dict(SOURCE, title='Original title',
                                messages=[{'role': 'user', 'text': TEXT}, {'role': 'assistant', 'text': 'Original reply'}])),
}
REGISTRY = build_registry()
WRITES = tuple(name for name in NATIVE if REGISTRY.get(name).kind == 'scoped_write')
SCOPED_BODIES = tuple(name for name, (_, body) in NATIVE.items() if 'read_scopes' in body)
LEGACY = [
    ('GET', '/search?q=fixture&type=all&hall=project&limit=20&rerank=true&graph=false', ''),
    ('GET', '/counts/knowledge', ''), ('GET', '/messages/counts/by-agent-role', ''),
    ('GET', '/graph/search?q=fixture', ''), ('GET', '/graph/entities/' + ID, ''),
    ('GET', '/artifacts', ''), ('GET', '/artifacts/' + ID + '/source', ''),
    ('POST', '/knowledge/ingest', '{}'), ('POST', '/sessions/ingest', '{}'),
    ('GET', '/sessions/ingested-ids', ''), ('POST', '/artifacts', '{}'),
    ('POST', '/artifacts/parse-document', '{}'), ('POST', '/artifacts/transcribe', '{}'),
    ('POST', '/artifacts/describe-image', '{}'), ('POST', '/knowledge/' + ID + '/invalidate', '{}'),
]


def arguments(operation):
    result = {'payload': dict(NATIVE[operation][1])}
    if operation in WRITES:
        result['idempotency_key'] = 'fixture-explicit-key'
    return result


@pytest.mark.parametrize('operation', NATIVE)
def test_native_request_model_rejects_extra_fields_before_key(operation):
    client, reader, calls = recording_client()
    kwargs = arguments(operation)
    kwargs['payload']['principal'] = 'unissued-private-marker'
    with pytest.raises(ClientError) as refused:
        client.call(operation, **kwargs)
    assert 'unissued-private-marker' not in str(refused.value)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('field,value', [('query', ''), ('query', '\x00'), ('query', 'x' * 513),
    ('intent', 'legacy-rerank'), ('limit', True), ('limit', '25'), ('limit', 26),
    ('graph_hops', 3), ('deadline_ms', 49), ('include_stale', 'true')])
def test_native_search_modes_and_bounds_refuse_before_key(field, value):
    client, reader, calls = recording_client()
    kwargs = arguments('memory.search')
    kwargs['payload'][field] = value
    with pytest.raises(ClientError):
        client.call('memory.search', **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('operation', SCOPED_BODIES)
@pytest.mark.parametrize('scopes', [['other'], [PROJECT, 'other'], []])
def test_body_read_scope_cannot_widen_selected_member_before_key(operation, scopes):
    client, reader, calls = recording_client()
    kwargs = arguments(operation)
    kwargs['payload']['read_scopes'] = scopes
    with pytest.raises(ClientError):
        client.call(operation, **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('operation', WRITES)
@pytest.mark.parametrize('key', [None, '', 'x' * 129, ' ', ' key', 'key ', 'unissued\r\nmarker'])
def test_native_ingest_and_retrieval_writes_require_preserved_explicit_key(operation, key):
    client, reader, calls = recording_client()
    kwargs = arguments(operation)
    kwargs['idempotency_key'] = key
    with pytest.raises(ClientError):
        client.call(operation, **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('operation', NATIVE)
def test_undeclared_query_arguments_are_not_silently_ignored(operation):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call(operation, **arguments(operation), query={'legacy_mode': 'unmapped'})
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('run_id', ['12345678', '../other', ID + '?scope=other', ID + '#fragment'])
def test_ingest_run_path_requires_exact_uuid_before_key(run_id):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('ingest.run', path_params={'run_id': run_id})
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('operation', NATIVE)
def test_native_loopback_keeps_original_payload_data_and_metadata(wire, operation):
    data = {'fixture_original': TEXT, 'hits': [{'content_id': ID, 'revision': 3}],
            'coverage': {'read_scopes': [PROJECT], 'selected_scope': PROJECT},
            'degraded': ['vectors_not_configured'], 'worker_state': 'unavailable',
            'lineage': {'replaced_run_id': ID, 'source_key': SOURCE['source_key']}}
    origin, records = wire(body={'data': data, 'request_id': 'fixture-body-request'}, replay=operation in WRITES)
    reader = FixtureReader()
    result = CortexClient(member_profile(origin, reader)).call(operation, **arguments(operation))
    assert records == [{'path': NATIVE[operation][0], 'body': NATIVE[operation][1],
                        'scope': PROJECT, 'idempotency': 'fixture-explicit-key' if operation in WRITES else None}]
    assert result.data == data and result.request_id == 'fixture-body-request'
    assert result.replayed is (operation in WRITES) and result.key_expires_at == EXPIRY
    assert reader.reads == 1


def test_native_ingest_run_get_uses_exact_id_and_no_write_key(receiver):
    origin, records = receiver()
    reader = FixtureReader()
    result = CortexClient(member_profile(origin, reader)).call('ingest.run', path_params={'run_id': ID})
    assert result.data == {'ok': True} and result.key_expires_at == EXPIRY
    assert records[0]['path'] == '/v1/ingest/runs/' + ID
    assert records[0]['method'] == 'GET' and records[0]['idempotency'] is None
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('status,code', [(403, 'scope_read_denied'), (403, 'writer_policy_denied'),
    (409, 'idempotency_key_reused'), (429, 'queue_budget_exhausted'), (503, 'outcome_unknown'),
    (422, 'transcript_parse_failed_quarantined')])
def test_native_refusals_preserve_code_quarantine_fields_and_no_retry(wire, status, code):
    fields = [{'path': 'run_id', 'type': 'quarantine_run', 'value': ID},
              {'path': 'transcript', 'type': 'malformed_json', 'line': 2}]
    origin, records = wire(status=status, body={'error': {'code': code, 'message': 'Fixture refusal',
        'retryable': status == 503, 'fields': fields}})
    reader = FixtureReader()
    with pytest.raises(CortexApiError) as refused:
        CortexClient(member_profile(origin, reader)).call('ingest.session', **arguments('ingest.session'))
    assert refused.value.code == code and refused.value.fields == fields
    assert refused.value.key_expires_at == EXPIRY and refused.value.request_id == 'fixture-request'
    assert reader.reads == 1 and len(records) == 1


@pytest.mark.parametrize('method,path,body', LEGACY)
def test_unmapped_search_ingest_stops_before_profile_key_http(monkeypatch, method, path, body):
    def forbidden(*args, **kwargs):
        pytest.fail('unmapped surface must stop before profile/key/HTTP')
    monkeypatch.setattr(bridge, 'load_member_profile', forbidden)
    monkeypatch.setattr(bridge, 'http_request', forbidden)
    out, err = io.StringIO(), io.StringIO()
    code = bridge.main(['api', method, path], stdin=io.StringIO(body), stdout=out, stderr=err)
    assert (code, out.getvalue(), err.getvalue()) == (2, '', 'ERROR: facade request unavailable in this release\n')


@pytest.mark.parametrize('method,path,body', LEGACY)
def test_release_helper_reports_same_unavailable_contract(method, path, body):
    p = subprocess.run(['bash', '-c', 'source "$1"; cortex_api_call "$2" "$3" "$4"',
                        'fixture', str(ROOT / 'scripts/agent-shims/_cortex_api.sh'), method, path, body],
                       capture_output=True, text=True)
    assert (p.returncode, p.stdout, p.stderr) == (2, '', 'ERROR: facade request unavailable in this release\n')
