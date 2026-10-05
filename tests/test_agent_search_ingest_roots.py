"""Verifier follow-up: native model roots cannot be coerced or silently dropped."""
from collections import UserDict

import pytest

from cortex_v2.clients.errors import ClientError
from test_agent_search_ingest_contract import ID, NATIVE, arguments
from test_agent_coordination_write_contract import recording_client


class FalseMapping(dict):
    def __bool__(self):
        return False


@pytest.mark.parametrize('operation', NATIVE)
def test_post_array_root_is_not_coerced_into_an_accepted_object(operation):
    client, reader, calls = recording_client()
    kwargs = arguments(operation)
    kwargs['payload'] = list(kwargs['payload'].items())
    with pytest.raises(ClientError):
        client.call(operation, **kwargs)
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('payload', ['', False, 0, ()])
def test_non_object_empty_model_root_is_refused_before_private_access(payload):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('memory.rebuild-projections', payload=payload, idempotency_key='fixture-key')
    assert reader.reads == 0 and calls == []


@pytest.mark.parametrize('field', ['query', 'path_params'])
@pytest.mark.parametrize('value', [[], '', False, 0])
def test_non_mapping_argument_root_is_not_dropped(field, value):
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('memory.search', **arguments('memory.search'), **{field: value})
    assert reader.reads == 0 and calls == []


def test_falsey_payload_mapping_preserves_valid_native_fields():
    client, reader, calls = recording_client()
    body = FalseMapping(NATIVE['memory.search'][1])
    client.call('memory.search', payload=body)
    assert calls[0][1]['json_body'] == dict(body)
    assert reader.reads == 1 and len(calls) == 1


def test_falsey_path_mapping_preserves_valid_run_id():
    client, reader, calls = recording_client()
    client.call('ingest.run', path_params=FalseMapping(run_id=ID))
    assert calls[0][0][1].endswith('/v1/ingest/runs/' + ID)
    assert reader.reads == 1 and len(calls) == 1


def test_falsey_query_mapping_does_not_hide_undeclared_query():
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('memory.search', **arguments('memory.search'), query=FalseMapping(legacy_mode='unmapped'))
    assert reader.reads == 0 and calls == []


def test_valid_mapping_api_accepts_userdict_without_changing_original_fields():
    client, reader, calls = recording_client()
    body = UserDict(NATIVE['memory.search'][1])
    client.call('memory.search', payload=body, path_params=UserDict(), query=UserDict())
    assert calls[0][1]['json_body'] == dict(body)
    assert reader.reads == 1 and len(calls) == 1


@pytest.mark.parametrize('payload', [None, {}])
def test_none_and_empty_object_still_support_the_native_empty_request_model(payload):
    client, reader, calls = recording_client()
    client.call('memory.rebuild-projections', payload=payload, idempotency_key='fixture-key')
    assert calls[0][1]['json_body'] == {}
    assert reader.reads == 1 and len(calls) == 1


def test_none_does_not_supply_missing_required_native_fields():
    client, reader, calls = recording_client()
    with pytest.raises(ClientError):
        client.call('memory.search', payload=None)
    assert reader.reads == 0 and calls == []
