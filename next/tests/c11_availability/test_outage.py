"""C11 real-PostgreSQL interruption contract; the host controls only its owned PG."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest
from support import require_probe
from uuid import UUID
import psycopg
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import API, Fixture, READ_A, REQUEST, WRITE_A, uid
from cortex_core.api import c11a
from cortex_core.api.c11b import C11bRecordPort

def control(action):
    print('CORTEX_CONTROL=' + action, flush=True)

class AvailabilityAPI(Fixture, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')
        self.port = C11bRecordPort(self.connection, UUID(uid(1)), {'3': UUID(uid(3))})
        self.effects = []
        self.observations = []
        self.probes = []
        self.probe_dsn = os.environ['TEST_PROBE_DATABASE_URL']

    def connection(self):
        db = psycopg.connect(self.dsn, autocommit=True, connect_timeout=1)
        db.execute(f'SET ROLE "{REQUEST}"')
        return db

    def database_up(self):
        try:
            with self.connection() as db:
                return db.execute('SELECT 1').fetchone() == (1,)
        except psycopg.Error:
            return False

    def wait_database(self, expected):
        for _ in range(50):
            if self.database_up() is expected:
                return
            time.sleep(0.1)
        self.fail('owned PostgreSQL did not reach expected availability')

    def gateway(self, probe=None, *, record_reader=None, writer=None, supervisor_down=False):
        if probe is None:
            factory, make_probe = require_probe(self)
            probe = make_probe(factory(self.probe_dsn), timeout_seconds=0.2)
            self.probes.append(probe)

        async def health():
            up = await probe()
            return {'component': 'cortex', 'status': 'ok' if up else 'unavailable', 'readiness': 'enrollment_required' if up else 'unavailable'}

        async def state(*_):
            self.observations.append('capability')
            return {'state': 'unavailable'}

        async def principal(scope):
            self.observations.append('principal')
            return {**await self.port.principal(scope), '_request_scope': scope}

        async def grant(identity, _capability):
            self.observations.append('permission')
            scope = {**identity['_request_scope'], 'method': 'GET'}
            current = await self.port.principal(scope)
            return all((current[name] == identity[name] for name in ('principal_id', 'project_id', 'tenant_id', 'permission_generation')))

        async def memory(principal, scope, key):
            self.effects.append('memory')
            return await (writer or self.port.write_memory)(principal, scope, key)

        async def read(principal, scope, record_id):
            self.effects.append('record')
            return await (record_reader or self.port.read_record)(principal, scope, record_id)

        async def supervisor(_principal, _scope, _key):
            return {'core_available': True, 'state': 'supervisor_down'}
        handlers = {'C01-R102': memory}
        if supervisor_down:
            handlers['C01-R030'] = supervisor
        return c11a.ConsumerGateway(core_probe=probe, principal_resolver=principal, permission_recheck=grant, capability_source=state, health=health, handlers=handlers, record_reader=read, allow_legacy_idempotency=True)

    async def call(self, gateway, method, path, *, key=READ_A, body=None, request_key=None, drop_reply=False):
        payload = json.dumps(body, separators=(',', ':')).encode() if body is not None else b''
        headers = [(b'authorization', b'Bearer ' + key), (b'x-project', b'3'), (b'x-agent-name', b'10')]
        if request_key is not None:
            headers.append((b'idempotency-key', request_key.encode()))
        scope = {'type': 'http', 'method': method, 'path': path, 'headers': headers, 'query_string': b'', 'client': ('127.0.0.1', 10001)}
        messages = []

        async def receive():
            return {'type': 'http.request', 'body': payload, 'more_body': False}

        async def send(message):
            if drop_reply:
                raise ConnectionError('synthetic reply lost after handler completion')
            messages.append(message)

        async def invoke():
            await asyncio.wait_for(gateway.app(scope, receive, send), timeout=3)
        await invoke()
        status = next((item['status'] for item in messages if item['type'] == 'http.response.start'))
        data = b''.join((item.get('body', b'') for item in messages if item['type'] == 'http.response.body'))
        return (status, json.loads(data))

    def counts(self, record_id):
        return (self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s', (record_id,)).fetchone()[0], self.admin.execute('SELECT count(*) FROM coordination.outbox WHERE aggregate_id=%s', (record_id,)).fetchone()[0])

    async def test_owned_pg_outage_matrix_inflight_and_one_replay(self):
        body = {'section': 'decisions', 'content': 'healthy control', 'source': 'c11/availability/healthy'}

        async def local_db_probe():
            return await asyncio.to_thread(self.database_up)
        probe_factory = getattr(c11a, 'postgres_core_probe', None)
        probe = probe_factory(c11a.PostgresProbeFactory(self.probe_dsn), timeout_seconds=0.2) if callable(probe_factory) and callable(getattr(c11a, 'PostgresProbeFactory', None)) else local_db_probe
        if callable(getattr(probe, 'aclose', None)):
            self.probes.append(probe)
        gateway = self.gateway(probe)
        status, first = await self.call(gateway, 'POST', '/memory', key=WRITE_A, body=body, request_key='availability-healthy')
        self.assertEqual(status, 200)
        self.assertEqual(self.counts(first['id']), (1, 1))
        self.assertEqual((await self.call(gateway, 'GET', '/records/' + first['id']))[0], 200)
        self.assertEqual((await self.call(gateway, 'GET', '/health'))[0], 200)
        control('STOP')
        try:
            self.wait_database(False)
            self.assertTrue(callable(probe_factory), 'C11 lacks a database-backed Core probe')
            gateway = self.gateway(probe)
            routes = json.loads((Path(__file__).resolve().parents[2] / 'contracts/c11a-consumer-routes.json').read_text())['routes']
            self.assertEqual(routes, c11a.load_routes())
            before = len(self.effects)
            observed_before = len(self.observations)
            checked = []
            for row in routes:
                if row['path'] == '/health':
                    continue
                path = re.sub('\\{[^{}]+\\}', uid(7), row['path'])
                status, response = await self.call(gateway, row['method'], path, key=WRITE_A, body={'section': 'decisions', 'content': 'must not commit', 'source': 'c11/availability/no-effect'}, request_key='availability-down-' + row['id'])
                self.assertEqual((status, response.get('error', {}).get('code')), (503, 'core_unavailable'), row['id'])
                self.assertNotIn('receipt', response)
                checked.append(row['id'])
            self.assertEqual(len(checked), len(routes) - 1)
            self.assertEqual(len(self.effects), before)
            self.assertEqual(len(self.observations), observed_before)
            status, response = await self.call(gateway, 'GET', '/never-routed')
            self.assertEqual((status, response.get('error', {}).get('code')), (503, 'core_unavailable'))
            status, response = await self.call(gateway, 'GET', '/health')
            self.assertEqual((status, response['status']), (503, 'unavailable'))
            print('CORTEX_ROUTE_COUNT=' + str(len(checked)), flush=True)
        finally:
            control('START')
            self.wait_database(True)

        async def interrupted_read(principal, scope, record_id):
            control('STOP')
            await asyncio.to_thread(self.wait_database, False)
            return await self.port.read_record(principal, scope, record_id)
        try:
            status, response = await self.call(self.gateway(probe, record_reader=interrupted_read), 'GET', '/records/' + first['id'])
            self.assertEqual((status, response.get('error', {}).get('code')), (503, 'core_unavailable'))
            self.assertNotIn('id', response)
        finally:
            control('START')
            self.wait_database(True)
        pending = {'section': 'decisions', 'content': 'in-flight write', 'source': 'c11/availability/inflight'}

        async def interrupted_write(principal, scope, key):
            control('STOP')
            await asyncio.to_thread(self.wait_database, False)
            return await self.port.write_memory(principal, scope, key)
        try:
            status, response = await self.call(self.gateway(probe, writer=interrupted_write), 'POST', '/memory', key=WRITE_A, body=pending, request_key='availability-inflight')
            self.assertEqual((status, response.get('error', {}).get('code')), (503, 'core_unavailable'))
            self.assertNotIn('receipt', response)
        finally:
            control('START')
            self.wait_database(True)
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.addCleanup(self.admin.close)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.idempotency WHERE request_key=%s', ('availability-inflight',)).fetchone()[0], 0)
        gateway = self.gateway(probe)
        status, replay = await self.call(gateway, 'POST', '/memory', key=WRITE_A, body=pending, request_key='availability-inflight')
        self.assertEqual(status, 200)
        self.assertEqual(self.counts(replay['id']), (1, 1))
        self.assertEqual(await self.call(gateway, 'POST', '/memory', key=WRITE_A, body=pending, request_key='availability-inflight'), (200, replay))
        self.assertEqual(self.counts(replay['id']), (1, 1))
        self.assertEqual((await self.call(gateway, 'GET', '/records/' + replay['id']))[0], 200)
        status_gateway = self.gateway(probe, supervisor_down=True)
        status, observation = await self.call(status_gateway, 'GET', '/workers/health')
        self.assertEqual((status, observation['supervisor']['state']), (200, 'supervisor_down'))
        status, canonical = await self.call(status_gateway, 'POST', '/memory', key=WRITE_A, body={'section': 'decisions', 'content': 'Core remains writable', 'source': 'c11/availability/supervisor'}, request_key='availability-supervisor')
        self.assertEqual((status, self.counts(canonical['id'])), (200, (1, 1)))
        lost = {'section': 'decisions', 'content': 'committed but reply lost', 'source': 'c11/availability/lost-reply'}
        with self.assertRaises(ConnectionError):
            await self.call(gateway, 'POST', '/memory', key=WRITE_A, body=lost, request_key='availability-lost-reply', drop_reply=True)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.idempotency WHERE request_key=%s', ('availability-lost-reply',)).fetchone()[0], 1)
        status, resolved = await self.call(gateway, 'POST', '/memory', key=WRITE_A, body=lost, request_key='availability-lost-reply')
        self.assertEqual((status, self.counts(resolved['id'])), (200, (1, 1)))
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s', (hashlib.sha256(READ_A).hexdigest(),))
        status, response = await self.call(gateway, 'GET', '/records/' + replay['id'])
        self.assertEqual((status, response['error']['code']), (404, 'not_found'))

    async def asyncTearDown(self):
        deadline = asyncio.get_running_loop().time() + 2
        for probe in self.probes:
            await probe.aclose(deadline=deadline)
