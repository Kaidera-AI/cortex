"""Vera D2-AUTH-ORDER-001: credential resolution precedes POST body parsing.

Adapted from repro_auth_precedence.py (SHA-256 90caa799f3f90fdb14dfcc373f4f5682e9fe10e5fddf91620a1f458c03c22d87).
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from cortex_core.api import c11a


class MissingCredential(Exception):
    code = 'unauthenticated'


class RefusedCredential(Exception):
    code = 'forbidden'


class AuthPrecedence(unittest.TestCase):
    def response(self, error, payload):
        reads = []

        async def core():
            return True

        async def principal(_scope):
            raise error

        async def other(*_args):
            return {'state': 'ready'}

        async def receive():
            reads.append(True)
            return {'type': 'http.request', 'body': payload, 'more_body': False}

        sent = []

        async def send(message):
            sent.append(message)

        route = {'id': 'C01-R022', 'method': 'POST', 'path': '/search',
                 'effect': 'read', 'capability': 'pg_search'}
        with patch.object(c11a, 'load_routes', return_value=[route]):
            gateway = c11a.ConsumerGateway(
                core_probe=core, principal_resolver=principal,
                permission_recheck=other, capability_source=other,
                health=other, handlers={'C01-R022': other},
                parse_search_body=True,
            )
        asyncio.run(gateway.app(
            {'type': 'http', 'method': 'POST', 'path': '/search', 'headers': []},
            receive, send,
        ))
        return sent[0]['status'], json.loads(sent[1]['body']), len(reads)

    def test_missing_credential_precedes_malformed_ordinary_search_body(self):
        status, body, reads = self.response(MissingCredential(), b'{invalid-json')
        self.assertEqual((status, body['error']['code']),
                         (401, 'credential_required'))
        self.assertEqual(reads, 0)

    def test_presented_refusal_with_d2_target_keeps_not_found(self):
        payload = json.dumps({'query': 'fixture',
                              'after': {'record_id': '00000000-0000-0000-0000-000000000001',
                                        'revision': 1}}).encode()
        status, body, _ = self.response(RefusedCredential(), payload)
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


if __name__ == '__main__':
    unittest.main()
