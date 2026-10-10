"""D2 target wait through the real C04 and PostgreSQL search bindings."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
import hashlib

from core_db_fixture import READ_A, uid
from cortex_core.embeddings.pg_search import PostgresSearch
from test_read_api import FunctionProvider, PROBE_VECTOR, ReadAPI


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

    async def d2_call(self, after=None, wait_ms=0, *, disconnect=None,
                      body=None, emitted=None, key=READ_A):
        if body is None:
            body = {'query': 'fixture', 'top_k': 5, 'after': after,
                    'wait_ms': wait_ms}
        payload = json.dumps(body).encode()
        scope = {'type': 'http', 'method': 'POST', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + key),
                             (b'x-project', b'3')], 'query_string': b''}
        sent = False
        messages = [] if emitted is None else emitted

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
        other = uid(7)
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES (%s,%s,%s,%s,%s)',
                           (*self.scope, other, 'memory', 99))
        self.admin.execute('INSERT INTO retrieval.search_vectors VALUES '
                           '(%s,%s,%s,%s,%s,%s::vector)',
                           (*self.scope, other, 99, self.read_port.identity.key,
                            '[' + ','.join(map(str, PROBE_VECTOR)) + ']'))
        status, body = self.response(await self.d2_call(target))
        self.assertEqual((status, body['error']['code'], body['error'].get('state')),
                         (503, 'capability_unavailable', 'pending'))
        self.assertNotIn('results', body)

        self.admin.execute('UPDATE retrieval.search_vectors SET source_revision=2 '
                           'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                           (*self.scope, uid(6)))
        status, body = self.response(await self.d2_call(target))
        self.assertEqual(status, 200)
        self.assertEqual((body['complete'], body['state'], body['freshness']['complete']),
                         (True, 'ready', True))
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

    async def test_presented_credential_refusals_match_unknown_target(self):
        target = {'record_id': uid(6), 'revision': 1}
        unknown = await self.d2_call({'record_id': uid(99), 'revision': 1})
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                           'WHERE key_digest=%s',
                           (hashlib.sha256(READ_A).hexdigest(),))
        revoked = await self.d2_call(target)
        forged = await self.d2_call(target, key=b'synthetic-forged')
        self.assertEqual(revoked, unknown)
        self.assertEqual(forged, unknown)
        self.assertEqual(self.response(unknown)[0], 404)

    async def test_bounded_deadline_returns_pending_without_results(self):
        self.advance_source()
        target = {'record_id': uid(6), 'revision': 2}
        started = asyncio.get_running_loop().time()
        status, body = self.response(await self.d2_call(target, 150))
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual((status, body.get('error', {}).get('state')),
                         (503, 'pending'))
        self.assertNotIn('results', body)
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertLess(elapsed, 0.9)

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

    async def test_wait_advances_on_actual_target_receipt(self):
        self.advance_source()
        target = {'record_id': uid(6), 'revision': 2}
        observed = asyncio.Event()
        original = PostgresSearch.projection_revision

        async def signal(port, *args, **kwargs):
            value = await original(port, *args, **kwargs)
            if value.state == 'pending':
                observed.set()
            return value

        with patch.object(PostgresSearch, 'projection_revision', signal):
            waiting = asyncio.create_task(self.d2_call(target, 1000))
            try:
                await asyncio.wait_for(observed.wait(), 0.75)
                self.admin.execute('UPDATE retrieval.search_vectors SET source_revision=2 '
                                   'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                                   (*self.scope, uid(6)))
                status, body = self.response(await asyncio.wait_for(waiting, 1.0))
                self.assertEqual(status, 200)
                self.assertEqual(body['freshness']['state'], 'current')
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def test_disconnect_and_late_revocation_cannot_emit_success(self):
        self.advance_source()
        target = {'record_id': uid(6), 'revision': 2}
        observed = asyncio.Event()
        disconnected = asyncio.Event()
        messages = []
        original = PostgresSearch.projection_revision

        async def signal(port, *args, **kwargs):
            value = await original(port, *args, **kwargs)
            if value.state == 'pending':
                observed.set()
            return value

        with patch.object(PostgresSearch, 'projection_revision', signal):
            waiting = asyncio.create_task(self.d2_call(
                target, 1000, disconnect=disconnected, emitted=messages))
            try:
                await asyncio.wait_for(observed.wait(), 0.75)
                disconnected.set()
                try:
                    await asyncio.wait_for(waiting, 0.5)
                except asyncio.CancelledError:
                    pass
                except TimeoutError:
                    self.fail('disconnect did not cancel the pending D2 wait')
                else:
                    self.fail('disconnect produced a late D2 response')
                self.assertEqual(messages, [])
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

        observed.clear()
        with patch.object(PostgresSearch, 'projection_revision', signal):
            waiting = asyncio.create_task(self.d2_call(target, 1000))
            try:
                await asyncio.wait_for(observed.wait(), 0.75)
                self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                                   'WHERE key_digest=%s',
                                   (hashlib.sha256(READ_A).hexdigest(),))
                self.admin.execute('UPDATE retrieval.search_vectors SET source_revision=2 '
                                   'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                                   (*self.scope, uid(6)))
                status, body = self.response(await asyncio.wait_for(waiting, 1.0))
                self.assertEqual((status, body['error']['code']), (404, 'not_found'))
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def test_target_shape_and_wait_cap_refuse_before_polling(self):
        target = {'record_id': uid(6), 'revision': 1}
        cases = [
            {'query': 'fixture', 'after': {**target, 'revision': True}},
            {'query': 'fixture', 'after': target, 'wait_ms': 10001},
            {'query': 'fixture', 'wait_ms': 1},
            {'query': 'fixture', 'min_revision': 1},
            {'query': 'fixture', 'after': {'record_id': 'wrong', 'revision': 1}},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, packet = self.response(await self.d2_call(body=body))
                self.assertEqual((status, packet.get('error', {}).get('code')),
                                 (400, 'invalid_input'))

    async def test_final_target_observation_blocks_late_tombstone(self):
        target = {'record_id': uid(6), 'revision': 1}

        async def tombstone(_query, _identity):
            with self.admin.transaction():
                self.admin.execute('INSERT INTO core.record_revisions VALUES '
                                   '(%s,%s,%s,2,%s,true)',
                                   (*self.scope, uid(6), uid(5)))
                self.admin.execute('UPDATE core.records SET current_revision=2,tombstone=true '
                                   'WHERE tenant_id=%s AND project_id=%s AND id=%s',
                                   (*self.scope, uid(6)))
            return PROBE_VECTOR

        async def allow_only_this_control(*_args, **_kwargs):
            return None

        self.read_port.provider = FunctionProvider(tombstone)
        with patch.object(self.read_port, '_post_read_freshness', allow_only_this_control):
            status, body = self.response(await self.d2_call(target))
        self.assertEqual((status, body.get('error', {}).get('state')),
                         (503, 'unavailable'))

    async def test_final_current_recheck_blocks_revocation_after_observation(self):
        target = {'record_id': uid(6), 'revision': 1}
        original = self.read_port._observe_target
        count = 0

        async def revoke_after_second_observation(*args):
            nonlocal count
            value = await original(*args)
            count += 1
            if count == 2:
                self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                                   'WHERE key_digest=%s',
                                   (hashlib.sha256(READ_A).hexdigest(),))
            return value

        with patch.object(self.read_port, '_observe_target', revoke_after_second_observation):
            status, body = self.response(await self.d2_call(target))
        self.assertEqual(count, 2)
        self.assertEqual((status, body.get('error', {}).get('code')),
                         (404, 'not_found'))


if __name__ == '__main__':
    import unittest
    unittest.main()
