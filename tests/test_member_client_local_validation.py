"""Author-verifier follow-up: local encoding fails before a member key read."""
from __future__ import annotations

import pytest

from cortex_v2.clients.client import CortexClient
from test_member_client_integration import FixtureReader, member_profile


class RefusedString:
    def __str__(self):
        raise ValueError('unencodable public fixture query')


@pytest.mark.parametrize('invalid', ('body', 'query-object', 'query-unicode'))
def test_local_encoding_refusal_never_reads_a_member_key(invalid):
    reader = FixtureReader()
    # No HTTP server is used: the selected path must fail during local encoding.
    client = CortexClient(member_profile('http://127.0.0.1:1', reader))
    if invalid == 'body':
        arguments = {'payload': {'body': object()}, 'idempotency_key': 'fixture-only'}
        operation = 'memory.record'
        error = TypeError
    else:
        arguments = {'query': {'fixture': RefusedString() if invalid == 'query-object' else '\ud800'}}
        operation = 'capability.discover'
        error = ValueError if invalid == 'query-object' else UnicodeEncodeError
    with pytest.raises(error):
        client.call(operation, **arguments)
    assert reader.reads == 0
