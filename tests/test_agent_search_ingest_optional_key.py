"""Verifier boundary: optional native read headers must also validate locally."""
import pytest

from cortex_v2.clients.errors import ClientError
from test_agent_search_ingest_contract import ID, NATIVE, WRITES, arguments
from test_agent_coordination_write_contract import recording_client

READS = [name for name in NATIVE if name not in WRITES] + ['ingest.run']


def read_arguments(operation):
    return arguments(operation) if operation != 'ingest.run' else {'path_params': {'run_id': ID}}


@pytest.mark.parametrize('operation', READS)
@pytest.mark.parametrize('key', ['', 'bad\r\nheader', ' key', 'x' * 129, '\u2603'])
def test_invalid_optional_read_header_refuses_before_member_key(operation, key):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call(operation, **read_arguments(operation), idempotency_key=key)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('operation', READS)
def test_safe_optional_read_key_is_preserved_without_becoming_required(operation):
    client, reader, calls = recording_client()
    client.call(operation, **read_arguments(operation), idempotency_key='fixture-optional-key')
    assert calls[0][1]['headers']['Idempotency-Key'] == 'fixture-optional-key'
    client.call(operation, **read_arguments(operation))
    assert 'Idempotency-Key' not in calls[1][1]['headers']
    assert reader.reads == 2 and len(calls) == 2
