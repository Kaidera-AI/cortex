"""Credentialed-only rule from Kai's 19:50 C11b ruling."""

import asyncio
import json
import unittest

from cortex_core.api.c11a import ConsumerGateway


class MissingCredential(Exception):
    code = 'unauthenticated'


class RevokedCredential(Exception):
    code = 'forbidden'


class CredentialGateway(unittest.TestCase):
    def request(self, error, *, core=True):
        async def ready(): return core
        async def principal(_): raise error
        async def recheck(*_): return False
        async def state(*_): return {'state': 'unavailable'}
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        app = ConsumerGateway(core_probe=ready, principal_resolver=principal,
            permission_recheck=recheck, capability_source=state, health=health,
            handlers={})
        scope = {'type': 'http', 'method': 'GET', 'path': '/projects/example',
                 'headers': [(b'x-project', b'example')], 'query_string': b''}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(value): messages.append(value)
        asyncio.run(app.app(scope, receive, send))
        status = next(x['status'] for x in messages if x['type'] == 'http.response.start')
        body = json.loads(next(x['body'] for x in messages if x['type'] == 'http.response.body'))
        return status, body

    def test_missing_credential_has_typed_upgrade_refusal(self):
        status, body = self.request(MissingCredential())
        self.assertEqual(status, 401)
        self.assertEqual(body['error']['code'], 'credential_required')
        self.assertIs(body['error']['retryable'], False)
        self.assertIn('upgrade', body['error']['message'].lower())

    def test_revoked_is_forbidden_and_core_down_precedes_credential(self):
        self.assertEqual(self.request(RevokedCredential())[0], 403)
        status, body = self.request(MissingCredential(), core=False)
        self.assertEqual((status, body['error']['code']), (503, 'core_unavailable'))
