"""RED-first released CLI graph job fields through C04/C05 and real PG."""

import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import API, Fixture, OWNER_A, READ_A, READ_B, REQUEST, WRITE_A, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.api.c11b2 import C11b2ReadPort, PROBE_VECTOR
from cortex_core.coordination import Jobs
from cortex_core.embeddings.pg_search import EmbeddingIdentity


IDENTITY = EmbeddingIdentity('fixture', 'frozen-query-vector', 'v1', 768, 'v1')
REPO = '/approved/project-3'


class Provider:
    async def embed(self, _query, _identity):
        return PROBE_VECTOR


class GraphJobRead(unittest.IsolatedAsyncioTestCase):
    reset = Fixture.reset
    auth = Fixture.auth

    async def asyncSetUp(self):
        Fixture.setUp(self)
        for scope in ((uid(2), uid(3)), (uid(21), uid(3))):
            generation = uuid4()
            self.admin.execute('INSERT INTO retrieval.graph_generations VALUES (%s,%s,%s,%s,now())',
                               (*scope, generation, 'fixture-extractor'))
            self.admin.execute('INSERT INTO retrieval.graph_state VALUES (%s,%s,%s,%s)',
                               (*scope, generation, 'ready'))
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')

        def connection():
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db

        async def init(conn):
            await conn.execute(f'SET ROLE "{REQUEST}"')

        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, init=init)
        self.ports = {}
        for installation in (1, 20):
            record = C11bRecordPort(connection, UUID(uid(installation)),
                                     {'3': UUID(uid(3))})
            port = C11b2ReadPort(record, self.pool, IDENTITY, Provider(),
                                  'fixture-extractor', {'3': REPO})
            async def ready(): return True
            async def health(): return {'component': 'cortex', 'status': 'ok'}
            gateway = ConsumerGateway(core_probe=ready,
                principal_resolver=port.principal,
                permission_recheck=port.permission_recheck,
                capability_source=port.capability_source,
                health=health, handlers=port.handlers)
            self.ports[installation] = gateway

    async def asyncTearDown(self):
        await self.pool.close()

    def jobs(self, key=OWNER_A):
        return Jobs(self.request, key, UUID(uid(1)), UUID(uid(3)))

    def create(self, number, kind='graph.build'):
        job_id = UUID(uid(number))
        self.jobs().create(job_id, kind, b'{"source":"synthetic"}', f'create-{number}')
        return job_id

    async def call(self, job_id, *, key=READ_A, installation=1):
        path = f'/graph/build/jobs/{job_id}'
        scope = {'type': 'http', 'method': 'GET', 'path': path,
                 'headers': [(b'authorization', b'Bearer ' + key),
                             (b'x-project', b'3')], 'query_string': b''}
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(value):
            messages.append(value)
        await self.ports[installation].app(scope, receive, send)
        status = next(row['status'] for row in messages if row['type'] == 'http.response.start')
        body = json.loads(next(row['body'] for row in messages
                               if row['type'] == 'http.response.body'))
        return status, body

    async def test_released_cli_reads_exact_status_and_repo(self):
        # Published KOS v0.2.007 .agents/scripts/cortex-graph-build,
        # SHA-256 b50d8a97...: lines 149-150 read status/repo; 162/166
        # terminate only for completed/failed.
        job_id = self.create(60)
        status, body = await self.call(job_id)
        self.assertEqual(status, 200, body)
        self.assertEqual((body['status'], body['repo']), ('queued', REPO))
        self.assertEqual(body['job_id'], str(job_id))
        claimed = self.jobs(WRITE_A).claim(job_id, 'claim-60')
        status, body = await self.call(job_id)
        self.assertEqual((status, body['status'], body['repo']), (200, 'running', REPO))
        self.jobs(WRITE_A).complete(job_id, claimed.attempt_id, claimed.fence,
                                    b'finished', 'complete-60')
        status, body = await self.call(job_id)
        self.assertEqual((status, body['status'], body['repo']), (200, 'completed', REPO))
        failed_id = self.create(61)
        failed_claim = self.jobs(WRITE_A).claim(failed_id, 'claim-61')
        self.jobs(WRITE_A).fail(failed_id, failed_claim.attempt_id, failed_claim.fence,
                                b'failure', 'fail-61')
        status, body = await self.call(failed_id)
        self.assertEqual((status, body['status'], body['repo']), (200, 'failed', REPO))

    async def test_other_tenant_same_id_and_missing_id_are_same_not_found(self):
        job_id = self.create(62)
        other = await self.call(job_id, key=READ_B, installation=20)
        missing = await self.call(UUID(uid(98)), key=READ_B, installation=20)
        self.assertEqual(other, missing)
        self.assertEqual(other, (404, {'error': {'code': 'not_found',
                                                'retryable': False}}))

    async def test_unrelated_job_kind_and_malformed_id_refuse(self):
        unrelated = self.create(63, kind='extract')
        status, body = await self.call(unrelated)
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))
        status, body = await self.call('not-a-uuid')
        self.assertEqual((status, body['error']['code']), (400, 'invalid_input'))

    async def test_revocation_after_c05_read_refuses_before_success(self):
        job_id = self.create(64)
        original = Jobs.get

        def revoke_after_read(jobs, value):
            result = original(jobs, value)
            self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() '
                               'WHERE key_digest=%s',
                               (hashlib.sha256(READ_A).hexdigest(),))
            return result

        with patch.object(Jobs, 'get', revoke_after_read):
            status, body = await self.call(job_id)
        self.assertEqual((status, body['error']['code']), (403, 'forbidden'))


if __name__ == '__main__':
    unittest.main()
