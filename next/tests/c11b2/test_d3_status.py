"""D3 status through real C04, search RLS, and the reviewed C08 lease source."""

import asyncio
import hashlib
import json
from unittest import IsolatedAsyncioTestCase
from uuid import UUID

import asyncpg

from core_db_fixture import READ_A, READ_B, REQUEST, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.api.c11b2 import C11b2ReadPort
from cortex_core.conductor.supervisor import Supervisor
from test_read_api import ReadAPI, IDENTITY


class D3Status(IsolatedAsyncioTestCase):
    reset = ReadAPI.reset
    auth = ReadAPI.auth

    async def asyncSetUp(self):
        await ReadAPI.asyncSetUp(self)
        from pathlib import Path
        schema = Path(__file__).resolve().parents[2] / 'schema/coordination/c08-supervisor-leases.sql'
        self.admin.execute(schema.read_text())
        self.admin.execute('''DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='c11d3_conductor') THEN
                CREATE ROLE c11d3_conductor LOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END $$''')
        self.admin.execute('GRANT USAGE ON SCHEMA coordination TO c11d3_conductor')
        self.admin.execute('GRANT SELECT,INSERT,UPDATE ON coordination.supervisor_leases TO c11d3_conductor')
        import os
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', 'c11d3_conductor@')
        self.control_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        self.supervisor = Supervisor(self.control_pool, UUID(uid(1)), lease_seconds=10)
        self.gateway.handlers['C01-R030'] = self.supervisor_status

    async def asyncTearDown(self):
        await self.control_pool.close()
        await ReadAPI.asyncTearDown(self)

    async def supervisor_status(self, _principal, _scope, _key):
        return await self.supervisor.health()

    async def call_as(self, key, path, method='GET', *, gateway=None):
        scope = {'type': 'http', 'method': method, 'path': path,
                 'headers': ([(b'authorization', b'Bearer ' + key)] if key is not None else [])
                            + [(b'x-project', b'3')], 'query_string': b''}
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(value):
            messages.append(value)
        await (gateway or self.gateway).app(scope, receive, send)
        start = next(x for x in messages if x['type'] == 'http.response.start')
        body = next(x for x in messages if x['type'] == 'http.response.body')
        return start['status'], json.loads(body['body'])

    async def test_scoped_search_facts_and_backlog_refusal(self):
        self.admin.execute('INSERT INTO retrieval.search_state VALUES (%s,%s,%s,%s)',
                           (uid(21), uid(3), IDENTITY.key, 'ready'))
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES (%s,%s,%s,%s,%s)',
                           (uid(21), uid(3), uid(6), 'memory', 1))
        await self.supervisor.acquire()
        record_b = C11bRecordPort(self.record_port.connection_factory, UUID(uid(20)),
                                  {'3': UUID(uid(3))})
        read_b = C11b2ReadPort(record_b, self.pool, IDENTITY, self.read_port.provider,
                               'fixture-extractor', {'3': '/approved/project-3'})
        gateway_b = self.make_gateway(read_b)
        supervisor_b = Supervisor(self.control_pool, UUID(uid(20)), lease_seconds=10)
        gateway_b.handlers['C01-R030'] = lambda _p, _s, _k: supervisor_b.health()
        a_status, a = await self.call_as(READ_A, '/degradation')
        b_status, b = await self.call_as(READ_B, '/degradation', gateway=gateway_b)
        self.assertEqual((a_status, b_status), (200, 200))
        self.assertEqual((a['search']['source_pending'], a['search']['core_unprojected']), (0, 0))
        self.assertEqual((b['search']['source_pending'], b['search']['core_unprojected']), (1, 0))
        self.assertNotIn(uid(21), json.dumps(a))
        self.assertEqual(a['canonical_write']['state'], 'unavailable')
        for key, gateway in ((READ_A, self.gateway), (READ_B, gateway_b)):
            status, body = await self.call_as(key, '/beat/embeddings/backlog', gateway=gateway)
            self.assertEqual((status, body['error']['code'], body['error']['reason']),
                             (503, 'capability_unavailable', 'backlog_source_unbound'))
            self.assertNotIn('pending', body)

    async def test_health_search_and_supervisor_are_independent(self):
        await self.supervisor.acquire()
        self.admin.execute('UPDATE retrieval.search_sources SET source_revision=2 '
                           'WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                           (uid(2), uid(3), uid(6)))
        self.assertEqual((await self.call_as(READ_A, '/health'))[0], 200)
        status, body = await self.call_as(READ_A, '/degradation')
        self.assertEqual(status, 200)
        self.assertEqual((body['search']['state'], body['search']['source_pending']), ('lagging', 1))
        self.assertEqual(body['supervisor']['state'], 'healthy')
        self.assertEqual(body['canonical_write']['state'], 'unavailable')
        status, body = await self.call_as(READ_A, '/workers/health')
        self.assertEqual((status, body['supervisor']['state']), (200, 'healthy'))
        self.assertEqual(body['workers']['state'], 'unavailable')
        self.assertNotIn('fence', body)
        await self.supervisor.release()
        status, body = await self.call_as(READ_A, '/workers/health')
        self.assertEqual((status, body['supervisor']['state']), (200, 'supervisor_down'))
        self.assertNotIn('fence', body)

    async def test_core_down_and_c04_refusals_precede_status(self):
        async def down(): return False
        async def health(): return {'component': 'cortex', 'status': 'unavailable'}
        gateway = ConsumerGateway(core_probe=down, principal_resolver=self.read_port.principal,
            permission_recheck=self.read_port.permission_recheck,
            capability_source=self.read_port.capability_source, health=health,
            handlers=self.gateway.handlers)
        self.assertEqual((await self.call_as(READ_A, '/health', gateway=gateway))[0], 503)
        for path in ('/degradation', '/workers/health', '/beat/embeddings/backlog'):
            status, body = await self.call_as(READ_A, path, gateway=gateway)
            self.assertEqual((status, body['error']['code']), (503, 'core_unavailable'))
        self.assertEqual((await self.call_as(None, '/workers/health'))[0], 401)
        self.assertEqual((await self.call_as(b'synthetic-forged', '/degradation'))[0], 403)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                           'WHERE key_digest=%s', (hashlib.sha256(READ_A).hexdigest(),))
        self.assertEqual((await self.call_as(READ_A, '/workers/health'))[0], 403)

    async def test_control_and_job_are_unimplemented_without_side_effect(self):
        calls = []
        async def side_effect(*_args):
            calls.append('called')
            return {'started': True}
        self.gateway.handlers['C01-R073'] = side_effect
        self.gateway.handlers['C01-R074'] = side_effect
        for method, path in (('POST', '/beat/embeddings/backfill'),
                             ('GET', '/beat/embeddings/jobs/' + uid(60))):
            status, body = await self.call_as(READ_A, path, method)
            self.assertEqual((status, body['error']['code'], body['error']['state']),
                             (503, 'capability_unavailable', 'unimplemented'))
        self.assertEqual(calls, [])


if __name__ == '__main__':
    import unittest
    unittest.main()
