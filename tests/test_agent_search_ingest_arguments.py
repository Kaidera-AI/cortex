"""Verifier follow-up: reject raw caller arguments the wire adapter would drop."""
import pytest

from cortex_v2.clients.errors import ClientError
from test_agent_search_ingest_contract import ID, NATIVE, arguments
from test_agent_coordination_write_contract import recording_client


@pytest.mark.parametrize('operation', list(NATIVE) + ['ingest.run'])
def test_undeclared_path_arguments_refuse_before_member_access(operation):
    client, reader, calls = recording_client()
    kwargs = arguments(operation) if operation != 'ingest.run' else {}
    kwargs['path_params'] = {'legacy_mode': 'unissued-unmapped-marker'}
    if operation == 'ingest.run':
        kwargs['path_params']['run_id'] = ID
    with pytest.raises(ClientError) as refused:
        client.call(operation, **kwargs)
    assert 'unissued-unmapped-marker' not in str(refused.value)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('payload', [{'legacy_mode': None}, {'read_scopes': None}, []])
def test_get_payload_is_not_silently_dropped_before_native_guard(payload):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('ingest.run', path_params={'run_id': ID}, payload=payload)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('payload', [None, {}])
def test_empty_native_get_arguments_remain_supported(payload):
    client, reader, calls = recording_client()
    client.call('ingest.run', path_params={'run_id': ID}, payload=payload)
    assert reader.reads == 1 and len(calls) == 1
