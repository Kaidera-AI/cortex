"""D2 target wait through the real C04 and PostgreSQL search bindings."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import UUID

from core_db_fixture import READ_A, uid
from cortex_core.embeddings.pg_search import PostgresSearch
from test_read_api import ReadAPI


class D2Wait(IsolatedAsyncioTestCase):
    reset = ReadAPI.reset
    auth = ReadAPI.auth
    asyncSetUp = ReadAPI.asyncSetUp
    asyncTearDown = ReadAPI.asyncTearDown
    make_gateway = ReadAPI.make_gateway
    call = ReadAPI.call

    def advance_source(self):
        with self.admin.transaction():
            self.admin.execute(
                'INSERT INTO core.record_revisions VALUES (%s,%s,%s,2,%s,false)',
                (*self.scope, uid(6), uid(5)),
            )
            self.admin.execute(
                'UPDATE core.records SET current_revision=2 '
                'WHERE tenant_id=%s AND project_id=%s AND id=%s',
                (*self.scope, uid(6)),
            )
            self.admin.execute(
                'UPDATE retrieval.search_sources SET source_revision=2 '
                'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                (*self.scope, uid(6)),
            )

    async def d2_call(self, after, wait_ms=0, *, disconnect=None):
        body = {'query': 'fixture', 'top_k': 5, 'after': after, 'wait_ms': wait_ms}
        payload = json.dumps(body).encode()
        scope = {'type': 'http', 'method': 'POST', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A),
                             (b'x-project', b'3')], 'query_string': b''}
        sent = False
        messages = []

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': payload, 'more_body': False}
            if disconnect is None:
                await asyncio.Future()
            else:
                await disconnect.wait()
                return {'type': 'http.disconnect'}

        async def send(value):
            messages.append(value)

        await self.gateway.app(scope, receive, send)
        return messages

    @staticmethod
    def response(messages):
        start = next(message for message in messages
                     if message['type'] == 'http.response.start')
        body = next(message for message in messages
                    if message['type'] == 'http.response.body')
        return start['status'], json.loads(body['body'])

    async def test_record_local_pending_then_complete_search(self):
        self.advance_source()
        target = {'record_id': uid(6), 'revision': 2}
        status, body = self.response(await self.d2_call(target))
        self.assertEqual((status, body['error']['code'], body['error'].get('state')),
                         (503, 'capability_unavailable', 'pending'))
        self.assertNotIn('results', body)

        self.admin.execute('UPDATE retrieval.search_vectors SET source_revision=2 '
                           'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                           (*self.scope, uid(6)))
        status, body = self.response(await self.d2_call(target))
        self.assertEqual(status, 200)
        self.assertEqual(body['freshness']['state'], 'current')
        self.assertTrue(any(hit['id'] == uid(6) and hit['revision'] == 2
                            for hit in body['results']))

    async def test_unknown_and_foreign_target_share_d1_404_and_tombstone_unavailable(self):
        foreign = uid(7)
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)",
                               (uid(21), uid(3), foreign))
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,1,%s,false)',
                               (uid(21), uid(3), foreign, uid(5)))
        unknown = await self.d2_call({'record_id': uid(99), 'revision': 1})
        forbidden = await self.d2_call({'record_id': foreign, 'revision': 1})
        self.assertEqual(forbidden, unknown)
        self.assertEqual(self.response(unknown)[0], 404)

        with self.admin.transaction():
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,2,%s,true)',
                               (*self.scope, uid(6), uid(5)))
            self.admin.execute('UPDATE core.records SET current_revision=2,tombstone=true '
                               'WHERE tenant_id=%s AND project_id=%s AND id=%s',
                               (*self.scope, uid(6)))
        status, body = self.response(await self.d2_call({'record_id': uid(6), 'revision': 1}))
        self.assertEqual((status, body['error']['state']), (503, 'unavailable'))

    async def test_pending_waits_release_pool_for_ordinary_search(self):
        self.advance_source()
        target = {'record_id': uid(6), 'revision': 2}
        pool_size = self.pool.get_max_size()
        observed = asyncio.Event()
        count = 0
        original = PostgresSearch.projection_revision

        async def counted(port, *args, **kwargs):
            nonlocal count
            result = await original(port, *args, **kwargs)
            if result.state == 'pending':
                count += 1
                if count >= pool_size:
                    observed.set()
            return result

        with patch.object(PostgresSearch, 'projection_revision', counted):
            waits = [asyncio.create_task(self.d2_call(target, 1000))
                     for _ in range(pool_size)]
            try:
                try:
                    await asyncio.wait_for(observed.wait(), 0.75)
                except TimeoutError:
                    self.fail('pool-size D2 waits never reached pending observations')
                self.assertTrue(all(not task.done() for task in waits))
                try:
                    ordinary = await asyncio.wait_for(
                        self.call('GET', '/search', query=b'q=fixture'), 0.5)
                except TimeoutError:
                    self.fail('ordinary search starved behind pending D2 waits')
                self.assertIn(ordinary[0], (200, 503))
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)


if __name__ == '__main__':
    import unittest
    unittest.main()
