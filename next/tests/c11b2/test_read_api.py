"""C11 gateway -> C04 -> Nemo ports through disposable real PostgreSQL."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from uuid import UUID, uuid4

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, API, READ_A, REQUEST, uid
from cortex_core.api.c11a import ConsumerGateway
from cortex_core.api.c11b import C11bRecordPort
from cortex_core.api.c11b2 import C11b2ReadPort, PROBE_VECTOR
from cortex_core.embeddings.pg_search import CapabilityUnavailable, EmbeddingIdentity
from cortex_core.modules.graph.pg_graph import GraphUnavailable
from cortex_core.auth import AuthError

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = EmbeddingIdentity('fixture', 'frozen-query-vector', 'v1', 768, 'v1')


class FunctionProvider:
    def __init__(self, function):
        self.function = function

    async def embed(self, query, identity):
        return await self.function(query, identity)


class ReadAPI(unittest.IsolatedAsyncioTestCase):
    reset = Fixture.reset
    auth = Fixture.auth

    async def asyncSetUp(self):
        Fixture.setUp(self)
        self.admin.execute(f'GRANT USAGE ON SCHEMA retrieval TO "{REQUEST}"')
        self.admin.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA retrieval TO "{REQUEST}"')
        self.admin.execute(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA retrieval TO "{REQUEST}"')
        self.admin.execute('INSERT INTO retrieval.search_state VALUES (%s,%s,%s,%s)',
                           (*self.scope, IDENTITY.key, 'ready'))
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES (%s,%s,%s,%s,%s)',
                           (*self.scope, uid(6), 'memory', 1))
        self.admin.execute('INSERT INTO retrieval.search_vectors VALUES (%s,%s,%s,%s,%s,%s::vector)',
                           (*self.scope, uid(6), 1, IDENTITY.key,
                            '[' + ','.join(map(str, PROBE_VECTOR)) + ']'))
        generation = uuid4()
        self.generation = generation
        self.admin.execute('INSERT INTO retrieval.graph_generations VALUES (%s,%s,%s,%s,now())',
                           (*self.scope, generation, 'fixture-extractor'))
        self.admin.execute('INSERT INTO retrieval.graph_state VALUES (%s,%s,%s,%s)',
                           (*self.scope, generation, 'ready'))
        dsn = os.environ['TEST_DATABASE_URL'].replace('postgres@', API + '@')
        def connection():
            import psycopg
            db = psycopg.connect(dsn, autocommit=True)
            db.execute(f'SET ROLE "{REQUEST}"')
            return db
        self.record_port = C11bRecordPort(connection, UUID(uid(1)), {'3': UUID(uid(3))})
        async def init(conn):
            await conn.execute(f'SET ROLE "{REQUEST}"')
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, init=init)
        async def vector(_query, _identity): return PROBE_VECTOR
        self.read_port = C11b2ReadPort(self.record_port, self.pool, IDENTITY,
                                       FunctionProvider(vector),
                                       'fixture-extractor', {'3': '/approved/project-3'})
        self.gateway = self.make_gateway(self.read_port)

    async def asyncTearDown(self):
        await self.pool.close()

    def make_gateway(self, port):
        async def ready(): return True
        async def health(): return {'component': 'cortex', 'status': 'ok'}
        return ConsumerGateway(core_probe=ready, principal_resolver=port.principal,
            permission_recheck=port.permission_recheck, capability_source=port.capability_source,
            health=health, handlers=port.handlers, parse_search_body=True)

    async def call(self, method, path, *, query=b'', body=None, key=READ_A, gateway=None):
        payload = json.dumps(body).encode() if body is not None else b''
        scope = {'type': 'http', 'method': method, 'path': path,
                 'headers': [(b'authorization', b'Bearer ' + key), (b'x-project', b'3')],
                 'query_string': query}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': payload, 'more_body': False}
        async def send(value): messages.append(value)
        await (gateway or self.gateway).app(scope, receive, send)
        status = next(x['status'] for x in messages if x['type'] == 'http.response.start')
        data = json.loads(next(x['body'] for x in messages if x['type'] == 'http.response.body'))
        return status, data

    async def test_real_search_envelopes_paging_and_lag_refusal(self):
        flags = self.admin.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='retrieval.search_state'::regclass").fetchone()
        self.assertEqual(flags, (True, True))
        status, page = await self.call('GET', '/search', query=b'q=fixture&limit=1')
        self.assertEqual(status, 200)
        self.assertEqual((len(page['results']), page['results'][0]['id']), (1, uid(6)))
        self.assertEqual((page['degraded'], page['freshness']['state']), (['rerank'], 'current'))
        status, post = await self.call('POST', '/search', body={'query': 'fixture', 'top_k': 1})
        self.assertEqual((status, post['results'][0]['id']), (200, uid(6)))
        status, unfused = await self.call('POST', '/search',
                                           body={'query': 'fixture', 'enable_graph': True})
        self.assertEqual((status, unfused['error']['code']), (503, 'capability_unavailable'))
        self.assertNotIn('results', unfused)
        self.assertEqual((await self.call('GET', '/search', query=b'q=fixture&limit=101'))[0], 400)
        self.admin.execute('UPDATE retrieval.search_sources SET source_revision=2 WHERE record_id=%s', (uid(6),))
        status, error = await self.call('GET', '/search', query=b'q=fixture')
        self.assertEqual((status, error['error']['code']), (503, 'capability_unavailable'))
        self.assertNotIn('results', error)
        scope = {'method': 'GET', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=fixture'}
        principal = await self.read_port.principal(scope)
        with self.assertRaises(CapabilityUnavailable):
            await self.read_port.search(principal, scope, None)

    async def test_unregistered_core_head_refuses_empty_success(self):
        body = b'{"text":"new committed head without search source"}'
        payload, rid = uuid4(), uuid4()
        self.admin.execute('INSERT INTO core.payloads VALUES (%s,%s,%s,%s,%s)',
                           (*self.scope, payload, body, hashlib.sha256(body).hexdigest()))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'memory',1,false)",
                               (*self.scope, rid))
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,1,%s,false)',
                               (*self.scope, rid, payload))
        scope = {'method': 'GET', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=absent'}
        principal = await self.read_port.principal(scope)
        state = await self.read_port.capability_source('pg_search', principal)
        self.assertEqual(state['state'], 'lagging')
        status, error = await self.call('GET', '/search', query=b'q=absent')
        self.assertEqual((status, error['error']['code']), (503, 'capability_unavailable'))
        self.assertNotIn('results', error)

    async def test_p01_provider_identity_cache_and_unavailable_refusal(self):
        calls = []
        class FakeProvider:
            async def embed(self, query, identity):
                calls.append((query, identity.provider, identity.model, identity.version,
                              identity.dimensions, identity.preprocessing, identity.key))
                return PROBE_VECTOR
        try:
            port = C11b2ReadPort(self.record_port, self.pool, IDENTITY, FakeProvider(),
                                 'fixture-extractor', {'3': '/approved/project-3'})
        except ValueError:
            self.fail('C11 must bind the P01 provider embed port')
        gateway = self.make_gateway(port)
        first = await self.call('GET', '/search', query=b'q=provider-probe', gateway=gateway)
        second = await self.call('GET', '/search', query=b'q=provider-probe', gateway=gateway)
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertEqual(calls, [('provider-probe', 'fixture', 'frozen-query-vector',
                                  'v1', 768, 'v1', IDENTITY.key)])
        self.assertEqual(first[1]['freshness']['identity'], IDENTITY.key)

        class Unavailable:
            async def embed(self, _query, _identity):
                raise CapabilityUnavailable('provider_unavailable')
        port = C11b2ReadPort(self.record_port, self.pool, IDENTITY, Unavailable(),
                             'fixture-extractor', {'3': '/approved/project-3'})
        status, error = await self.call('GET', '/search', query=b'q=unavailable',
                                        gateway=self.make_gateway(port))
        self.assertEqual((status, error['error']['code']), (503, 'capability_unavailable'))
        self.assertNotIn('results', error)

    async def test_installer_ledger_contains_nemo_retrieval_schemas(self):
        applied = {row[0] for row in self.admin.execute(
            'SELECT migration_id FROM core.schema_migrations').fetchall()}
        self.assertTrue({'retrieval-0001', 'retrieval-0002', 'retrieval-0003'} <= applied)
        for table in ('search_state', 'search_sources', 'search_vectors',
                      'query_embeddings', 'graph_state', 'graph_applied'):
            self.assertIsNotNone(self.admin.execute(
                'SELECT to_regclass(%s)', ('retrieval.' + table,)).fetchone()[0])
        flags = self.admin.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='retrieval' AND c.relname IN
            ('search_state','search_sources','search_vectors','query_embeddings',
             'graph_generations','graph_state','graph_applied','graph_nodes','graph_edges')""").fetchall()
        self.assertEqual(len(flags), 9)
        self.assertTrue(all(enabled and forced for _, enabled, forced in flags))

    async def test_graph_real_port_and_backlog_refusal(self):
        body = b'{"label":"fixture-concept","description":"current","content":"current"}'
        payload, rid, fact = uuid4(), uuid4(), uuid4()
        self.admin.execute('INSERT INTO core.payloads VALUES (%s,%s,%s,%s,%s)',
                           (*self.scope, payload, body, hashlib.sha256(body).hexdigest()))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'decision',1,false)",
                               (*self.scope, rid))
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,1,%s,false)',
                               (*self.scope, rid, payload))
        self.admin.execute('INSERT INTO core.extraction_facts '
                           '(tenant_id,project_id,id,record_id,source_revision,extractor_identity,payload_ref) '
                           'VALUES (%s,%s,%s,%s,1,%s,%s)',
                           (*self.scope, fact, rid, 'fixture-extractor', payload))
        self.admin.execute('INSERT INTO retrieval.graph_applied '
                           '(tenant_id,project_id,generation,record_id,source_revision,source_kind,source_label,source_description,fact_id) '
                           'VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s)',
                           (*self.scope, self.generation, rid, 'decision', 'fixture-concept', 'current', fact))
        self.admin.execute('INSERT INTO retrieval.graph_nodes VALUES (%s,%s,%s,%s,%s,%s,%s)',
                           (*self.scope, self.generation, rid, 'fixture-concept', 'concept', 'current'))
        status, graph = await self.call('GET', '/cortex-graph-search', query=b'q=fixture&limit=1')
        self.assertEqual((status, graph['freshness']['state'], graph['high_level'][0]['name']),
                         (200, 'current', 'fixture-concept'))
        status, stats = await self.call('GET', '/cortex-graph/stats')
        self.assertEqual((status, stats['entity_count']), (200, 1))
        self.assertEqual((await self.call('GET', '/cortex-graph/memory', query=b'limit=1001'))[0], 400)
        body = b'{"label":"pending","description":"","content":""}'
        payload = uuid4()
        rid = uuid4()
        self.admin.execute('INSERT INTO core.payloads VALUES (%s,%s,%s,%s,%s)',
                           (*self.scope, payload, body, hashlib.sha256(body).hexdigest()))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES (%s,%s,%s,'decision',1,false)",
                               (*self.scope, rid))
            self.admin.execute('INSERT INTO core.record_revisions VALUES (%s,%s,%s,1,%s,false)',
                               (*self.scope, rid, payload))
        status, error = await self.call('GET', '/cortex-graph-search', query=b'q=pending')
        self.assertEqual((status, error['error']['code']), (503, 'capability_unavailable'))
        self.assertNotIn('high_level', error)
        scope = {'method': 'GET', 'path': '/cortex-graph-search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=pending'}
        principal = await self.read_port.principal(scope)
        with self.assertRaises(GraphUnavailable):
            await self.read_port.graph_search(principal, scope, None)

    async def test_revocation_and_cancellation_cannot_emit_success(self):
        async def revoke(_query, _identity):
            self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                               (hashlib.sha256(READ_A).hexdigest(),))
            return PROBE_VECTOR
        revoked = C11b2ReadPort(self.record_port, self.pool, IDENTITY,
                                FunctionProvider(revoke),
                                'fixture-extractor', {'3': '/approved/project-3'})
        self.assertEqual((await self.call('GET', '/search', query=b'q=fixture',
                                          gateway=self.make_gateway(revoked)))[0], 403)
        self.admin.execute('UPDATE auth.credentials SET revoked_at=NULL WHERE key_digest=%s',
                           (hashlib.sha256(READ_A).hexdigest(),))
        started = asyncio.Event()
        async def stalled(_query, _identity):
            started.set()
            await asyncio.Event().wait()
        cancelled = C11b2ReadPort(self.record_port, self.pool, IDENTITY,
                                  FunctionProvider(stalled),
                                  'fixture-extractor', {'3': '/approved/project-3'})
        app = self.make_gateway(cancelled)
        scope = {'type': 'http', 'method': 'GET', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=fixture'}
        messages = []
        async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(value): messages.append(value)
        task = asyncio.create_task(app.app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(any(value['type'] == 'http.response.start' for value in messages))

    async def test_final_c04_recheck_refuses_revocation_after_nemo_query(self):
        scope = {'method': 'GET', 'path': '/search',
                 'headers': [(b'authorization', b'Bearer ' + READ_A), (b'x-project', b'3')],
                 'query_string': b'q=fixture'}
        principal = await self.read_port.principal(scope)
        original_pending = self.read_port._core_pending
        async def revoke_after_pending(search, subject):
            result = await original_pending(search, subject)
            self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                               (hashlib.sha256(READ_A).hexdigest(),))
            return result
        self.read_port._core_pending = revoke_after_pending
        with self.assertRaises(AuthError):
            await self.read_port.search(principal, scope, None)
