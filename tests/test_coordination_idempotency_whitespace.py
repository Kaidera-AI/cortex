"""Verifier follow-up: keys must survive HTTP field parsing unchanged."""
import pytest

from cortex_v2.clients.errors import ClientError
from test_agent_coordination_write_contract import arguments, recording_client


@pytest.mark.parametrize('key', [' ', '  ', ' fixture-key', 'fixture-key '])
def test_surrounding_or_only_spaces_refuse_before_member_key(key):
    client, reader, calls = recording_client()
    kwargs = arguments('return')
    kwargs['idempotency_key'] = key
    with pytest.raises(ClientError):
        client.call('coordination.handoff.return', **kwargs)
    assert reader.reads == 0 and calls == []


def test_valid_internal_space_key_is_preserved_exactly():
    client, reader, calls = recording_client()
    kwargs = arguments('return')
    kwargs['idempotency_key'] = 'fixture internal space'
    client.call('coordination.handoff.return', **kwargs)
    assert calls[0][1]['headers']['Idempotency-Key'] == 'fixture internal space'
    assert reader.reads == 1 and len(calls) == 1
