"""D4 ordinary search completeness through real scoped PostgreSQL API calls."""

import asyncio
import hashlib
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import UUID

from core_db_fixture import READ_A, READ_B, uid
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.api.c11b2 import C11b2ReadPort
from cortex_core.embeddings.pg_search import CapabilityUnavailable
from test_read_api import FunctionProvider, IDENTITY, PROBE_VECTOR, ReadAPI


class D4Complete(IsolatedAsyncioTestCase):
    reset = ReadAPI.reset
    auth = ReadAPI.auth
    asyncSetUp = ReadAPI.asyncSetUp
    asyncTearDown = ReadAPI.asyncTearDown
    make_gateway = ReadAPI.make_gateway
    call = ReadAPI.call
    _insert_pending_head = ReadAPI._insert_pending_head
    _gateway_lagging_after_ready = ReadAPI._gateway_lagging_after_ready

    def make_empty_a(self):
        self.admin.execute('DELETE FROM retrieval.search_vectors WHERE tenant_id=%s AND project_id=%s',
                           (uid(2), uid(3)))
        self.admin.execute('DELETE FROM retrieval.search_sources WHERE tenant_id=%s AND project_id=%s',
                           (uid(2), uid(3)))
        with self.admin.transaction():
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,2,%s,true)',
                               (uid(2), uid(3), uid(6), uid(5)))
            self.admin.execute('UPDATE core.records SET current_revision=2,tombstone=true '
                               'WHERE tenant_id=%s AND project_id=%s AND id=%s',
                               (uid(2), uid(3), uid(6)))

    async def test_ready_empty_get_post_and_nonempty_b_are_explicit_and_isolated(self):
        self.make_empty_a()
        record_b = C11bRecordPort(self.record_port.connection_factory, UUID(uid(20)),
                                  {'3': UUID(uid(3))})
        read_b = C11b2ReadPort(record_b, self.pool, IDENTITY, self.read_port.provider,
                               'fixture-extractor', {'3': '/approved/project-3'})
        self.admin.execute('INSERT INTO retrieval.search_state VALUES (%s,%s,%s,%s)',
                           (uid(21), uid(3), IDENTITY.key, 'ready'))
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES (%s,%s,%s,%s,%s)',
                           (uid(21), uid(3), uid(6), 'memory', 1))
        self.admin.execute('INSERT INTO retrieval.search_vectors VALUES (%s,%s,%s,%s,%s,%s::vector)',
                           (uid(21), uid(3), uid(6), 1, IDENTITY.key,
                            '[' + ','.join(map(str, PROBE_VECTOR)) + ']'))
        for method, kwargs in (('GET', {'query': b'q=empty'}),
                               ('POST', {'body': {'query': 'empty', 'top_k': 5}})):
            status, body = await self.call(method, '/search', **kwargs)
            self.assertEqual((status, body.get('complete'), body.get('state'), body.get('results')),
                             (200, True, 'ready_empty', []))
            self.assertEqual((body['freshness']['state'], body['freshness']['identity']),
                             ('current', IDENTITY.key))
            self.assertEqual(body['freshness']['model_identity']['model'], IDENTITY.model)
        status, body = await self.call('GET', '/search', query=b'q=fixture', key=READ_B,
                                       gateway=self.make_gateway(read_b))
        self.assertEqual((status, body.get('complete'), body.get('state')),
                         (200, True, 'ready'))
        self.assertEqual(len(body['results']), 1)

    async def test_lag_and_missing_canonical_head_never_become_empty_success(self):
        self.make_empty_a()
        self.admin.execute('UPDATE core.records SET current_revision=1,tombstone=false '
                           'WHERE tenant_id=%s AND project_id=%s AND id=%s',
                           (uid(2), uid(3), uid(6)))
        for method, kwargs in (('GET', {'query': b'q=empty'}),
                               ('POST', {'body': {'query': 'empty'}})):
            status, body = await self.call(method, '/search', **kwargs)
            self.assertEqual((status, body.get('error', {}).get('code')),
                             (503, 'capability_unavailable'))
            self.assertNotIn('results', body)
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES (%s,%s,%s,%s,%s)',
                           (uid(2), uid(3), uid(6), 'memory', 1))
        status, body = await self.call('GET', '/search', query=b'q=pending')
        self.assertEqual((status, body.get('error', {}).get('code')),
                         (503, 'capability_unavailable'))
        self.assertNotIn('results', body)

    async def test_late_lag_after_ready_gate_refuses_without_results(self):
        gateway, injected = await self._gateway_lagging_after_ready()
        status, body = await self.call('GET', '/search', query=b'q=late-lag', gateway=gateway)
        self.assertEqual(injected, ['pg_search'])
        self.assertEqual((status, body.get('error', {}).get('code')),
                         (503, 'capability_unavailable'))
        self.assertNotIn('results', body)

    async def test_disabled_rebuilding_provider_and_graph_fusion_refuse_without_results(self):
        for state in ('disabled', 'rebuilding'):
            self.admin.execute('UPDATE retrieval.search_state SET state=%s WHERE tenant_id=%s AND project_id=%s',
                               (state, uid(2), uid(3)))
            status, body = await self.call('GET', '/search', query=b'q=fixture')
            self.assertEqual((status, body.get('error', {}).get('code')),
                             (503, 'capability_unavailable'))
            self.assertNotIn('results', body)
        self.admin.execute("UPDATE retrieval.search_state SET state='ready' WHERE tenant_id=%s AND project_id=%s",
                           (uid(2), uid(3)))
        async def unavailable(_query, _identity):
            raise CapabilityUnavailable('provider_unavailable')
        port = C11b2ReadPort(self.record_port, self.pool, IDENTITY,
                             FunctionProvider(unavailable), 'fixture-extractor',
                             {'3': '/approved/project-3'})
        status, body = await self.call('GET', '/search', query=b'q=provider-down',
                                       gateway=self.make_gateway(port))
        self.assertEqual((status, body.get('error', {}).get('code')),
                         (503, 'capability_unavailable'))
        self.assertNotIn('results', body)
        status, body = await self.call('POST', '/search',
                                       body={'query': 'fixture', 'enable_graph': True})
        self.assertEqual((status, body.get('error', {}).get('code')),
                         (503, 'capability_unavailable'))
        self.assertNotIn('results', body)

    async def test_cancel_and_late_credential_or_project_change_never_emit_success(self):
        started = asyncio.Event()
        async def delayed(_query, _identity):
            started.set()
            await asyncio.Event().wait()
            return PROBE_VECTOR
        port = C11b2ReadPort(self.record_port, self.pool, IDENTITY,
                             FunctionProvider(delayed), 'fixture-extractor',
                             {'3': '/approved/project-3'})
        gateway = self.make_gateway(port)
        messages = []
        scope = {'type': 'http', 'method': 'GET', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=cancel-probe'}
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(value): messages.append(value)
        task = asyncio.create_task(gateway.app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(messages, [])

        original = C11b2ReadPort._post_read_freshness
        async def revoke_after_freshness(reader, *args, **kwargs):
            value = await original(reader, *args, **kwargs)
            self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                               'WHERE key_digest=%s', (hashlib.sha256(READ_A).hexdigest(),))
            return value
        with patch.object(C11b2ReadPort, '_post_read_freshness', revoke_after_freshness):
            status, body = await self.call('GET', '/search', query=b'q=revoke-late')
        self.assertEqual((status, body.get('error', {}).get('code')), (403, 'forbidden'))
        self.assertNotIn('results', body)

        self.admin.execute('UPDATE auth.credentials SET revoked_at=NULL WHERE key_digest=%s',
                           (hashlib.sha256(READ_A).hexdigest(),))
        async def change_project_after_freshness(reader, *args, **kwargs):
            value = await original(reader, *args, **kwargs)
            self.record_port.projects['3'] = UUID(uid(99))
            return value
        with patch.object(C11b2ReadPort, '_post_read_freshness', change_project_after_freshness):
            status, body = await self.call('GET', '/search', query=b'q=project-late')
        self.assertNotEqual(status, 200)
        self.assertNotIn('results', body)


if __name__ == '__main__':
    import unittest
    unittest.main()
