"""PG-D source/protocol witnesses. No PostgreSQL/native/installed authority credit."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
import hashlib
import importlib
import json
from types import SimpleNamespace
import unittest
from uuid import UUID

import asyncpg
from psycopg.pq import TransactionStatus

from cortex_core import auth
from cortex_core.api.c11b import C11bRecordPort, _credential
from cortex_core.api.c11b2 import C11b2ReadPort, PROBE_VECTOR
from cortex_core.embeddings.pg_search import (
    CoreUnavailable, CapabilityUnavailable, EmbeddingIdentity, PostgresSearch,
)
from cortex_core.embeddings.query_cache import QueryEmbeddingCache
from cortex_core.conductor.supervisor import LeaseBusy, StaleLease
from cortex_core.gateway.health_metrics import CoreSample


def uid(n):
    return UUID(int=n)


KEY_A, KEY_B = b'pgd-source-credential-A', b'pgd-source-credential-B'
SCOPE = auth.Scope(uid(1), uid(2), uid(3), uid(4), 7, 'read')
IDENTITY = EmbeddingIdentity('source', 'scripted', 'v1', 768, 'v1')
LOCAL_ROLE = 'SET LOCAL ROLE "kaidera-runtime-core-request"'


class Script:
    """Scripted replies and command trace, not an authorization/SQL implementation."""
    def __init__(self):
        self.scopes = {hashlib.sha256(KEY_A).hexdigest(): SCOPE,
                       hashlib.sha256(KEY_B).hexdigest(): replace(SCOPE, principal_id=uid(5))}
        self.events, self.connections, self.cache = [], [], {}
        self.active = self.borrowed = self.business = self.provider_calls = 0
        self.on_business = self.on_embed = None
        self.commit_error = self.clear_error = self.business_error = None
        self.advancing = self.empty = False
        self.projection = {}
        self.observe = {'database_bytes': 4096, 'pending_jobs': 8,
                        'pending_embedding_jobs': 2, 'lease': {'state': 'live', 'fence': 42}}
        self.observe_error = None
        self.lease_fence, self.lease_live = 42, True

    def connection(self):
        connection = AsyncConnection(self)
        self.connections.append(connection)
        return connection

    @asynccontextmanager
    async def acquire(self, **options):
        self.borrowed += 1
        connection = self.connection()
        self.active += 1
        self.events.append(('borrow', id(connection), options))
        try:
            yield connection
        finally:
            self.events.append(('release', id(connection), connection.in_transaction))
            self.active -= 1

    async def embed(self, _query, _identity):
        if self.active:
            raise AssertionError('PGD_PROVIDER_RETAINED_CONNECTION')
        self.provider_calls += 1
        if self.on_embed:
            await self.on_embed()
        return PROBE_VECTOR


class AsyncTransaction:
    def __init__(self, connection, options):
        self.connection, self.options = connection, options

    async def __aenter__(self):
        self.connection.in_transaction = True
        self.connection.script.events.append(('begin', id(self.connection), self.options))

    async def __aexit__(self, kind, value, traceback):
        connection, script = self.connection, self.connection.script
        connection.in_transaction = False
        script.events.append(('rollback' if kind else 'commit', id(connection)))
        if kind is None and script.commit_error:
            raise script.commit_error


class AsyncConnection:
    def __init__(self, script):
        self.script = script
        self.in_transaction = self.terminated = False
        self.context = dict.fromkeys(auth._CONTEXT, 'old')
        self.clear_calls = 0
        self.resolve_calls = 0
        self.scope = SCOPE
        self.exit_scope = None

    def is_closed(self):
        return self.terminated

    def is_in_transaction(self):
        return self.in_transaction

    def transaction(self, **options):
        return AsyncTransaction(self, options)

    def terminate(self):
        self.terminated = True
        self.script.events.append(('terminate', id(self)))

    def record(self, method, sql, arguments):
        self.script.events.append((method, sql, arguments, id(self), self.in_transaction))

    async def execute(self, sql, *arguments):
        self.record('execute', sql, arguments)
        if "set_config(name,'',false)" in sql:
            self.clear_calls += 1
            if self.clear_calls > 1 and self.script.clear_error:
                raise self.script.clear_error
            self.context.update(dict.fromkeys(arguments[0], ''))
        elif 'AS bound(name,value)' in sql:
            self.context.update(zip(*arguments))
        return 'OK'

    async def fetchval(self, sql, *arguments):
        self.record('fetchval', sql, arguments)
        script = self.script
        if 'auth.bind_scope' in sql or 'rolsuper OR rolbypassrls' in sql or 'pg_has_role' in sql:
            return 'rolsuper OR rolbypassrls' not in sql
        if 'runtime_lease_acquire' in sql or 'INSERT INTO coordination.supervisor_leases' in sql:
            return script.lease_fence
        if 'runtime_lease_assert_live' in sql:
            return script.lease_live
        if 'runtime_lease_renew' in sql or 'runtime_lease_release' in sql:
            return True
        if 'runtime_lease_health' in sql:
            return json.dumps(script.observe['lease'])
        if 'runtime_observe' in sql:
            if script.observe_error:
                raise script.observe_error
            return json.dumps(script.observe)
        if 'pg_database_size' in sql:
            return 4096
        if 'FROM coordination.jobs' in sql:
            return 8
        if 'count(*) FROM core.records' in sql:
            return 0
        if 'count(*) FROM retrieval.query_embeddings' in sql:
            return len(script.cache)
        if 'INSERT INTO retrieval.query_embeddings' in sql:
            key = tuple(arguments[:5]);script.cache[key] = {'vector': None, 'fresh': False}
            return 1
        if 'UPDATE retrieval.query_embeddings' in sql:
            key = tuple(arguments[:5]);script.cache[key] = {'vector': arguments[7], 'fresh': True}
            return 1
        if '::timestamptz > clock_timestamp()' in sql:
            return True
        if 'supervisor_leases' in sql:
            if 'EXTRACT' in sql:
                return 1
            return 42 if 'SELECT fence' in sql else True
        raise AssertionError('Unexpected scripted scalar query: '+sql)

    async def fetchrow(self, sql, *arguments):
        self.record('fetchrow', sql, arguments)
        script = self.script
        if 'auth.resolve_scope' in sql:
            self.resolve_calls += 1
            self.scope = (self.exit_scope if self.resolve_calls > 1 and self.exit_scope is not None
                          else script.scopes.get(arguments[0]))
            return asdict(self.scope) if self.scope else None
        if "current_setting('cortex.credential_digest'" in sql:
            return tuple(self.context[name] for name in auth._CONTEXT[:4])
        if 'FROM core.records r' in sql and 'LEFT JOIN retrieval.search_state' in sql:
            row = dict(current_revision=2, tombstone=False, kind='memory',
                       state_identity=IDENTITY.key, state='ready', desired_revision=2,
                       source_kind='memory', indexed_revision=2, vector_identity=IDENTITY.key)
            row.update(script.projection)
            return None if script.projection.get('missing_core') else row
        if 'FROM core.records' in sql:
            return dict(current_revision=1 if script.advancing else 2, tombstone=False, kind='memory')
        if 'FROM retrieval.search_state' in sql:
            return dict(identity=IDENTITY.key, state='ready')
        if 'AS indexed' in sql and 'retrieval.search_sources' in sql:
            return dict(indexed=0 if script.advancing else 1, pending=1 if script.advancing else 0)
        if 'FROM retrieval.search_sources s' in sql:
            return dict(desired_revision=2, kind='memory', indexed_revision=2, vector_identity=IDENTITY.key)
        if 'FROM retrieval.query_embeddings' in sql:
            return script.cache.get(tuple(arguments))
        if 'FROM coordination.supervisor_leases' in sql:
            return dict(expires_at='scripted', fence=42, live=True)
        raise AssertionError('Unexpected scripted row query: '+sql)

    async def fetch(self, sql, *arguments):
        self.record('fetch', sql, arguments)
        script = self.script
        script.business += 1
        if script.business_error:
            error, script.business_error = script.business_error, None
            raise error
        if script.on_business:
            await script.on_business(self)
        hit = dict(record_id=str(uid(6)), kind='memory', source_revision=2, distance=0.25)
        if 'LEFT JOIN LATERAL' in sql:
            return [dict(indexed=0 if script.empty else 1, pending=0,
                         **dict.fromkeys(hit, None))] if script.empty else [dict(indexed=1, pending=0, **hit)]
        if 'FROM retrieval.search_vectors' in sql:
            return [] if script.empty else [hit]
        raise AssertionError('Unexpected scripted rows query: '+sql)


class SyncCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class SyncConnection:
    closed, autocommit = False, True
    info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)

    def __init__(self):
        self.events, self.context = [], {}
        self.session_role, self.local_role = 'caller-selected-role', None

    def execute(self, sql, arguments=()):
        self.events.append((sql, arguments, self.local_role))
        if sql == LOCAL_ROLE:
            self.local_role = 'kaidera-runtime-core-request'
        elif "set_config(name,'',false)" in sql:
            self.context.update(dict.fromkeys(arguments[0], ''))
        elif 'AS bound(name,value)' in sql:
            self.context.update(zip(*arguments))
        elif 'auth.resolve_scope' in sql:
            return SyncCursor(tuple(asdict(replace(SCOPE, action=arguments[3])).values()))
        elif "current_setting('cortex.credential_digest'" in sql:
            return SyncCursor(tuple(self.context[n] for n in auth._CONTEXT[:4]))
        return SyncCursor((True,))

    def transaction(self):
        connection = self
        class Transaction:
            def __enter__(self):
                return self
            def __exit__(self, *error):
                connection.local_role = None
        return Transaction()

    def close(self):
        self.closed = True


class RecordPort(C11bRecordPort):
    def __init__(self, script):
        super().__init__(lambda: None, uid(1), {'p': uid(3), 'other': uid(30)})
        self.script = script

    async def principal(self, request):
        project = self._project(request)
        scope = self.script.scopes.get(hashlib.sha256(_credential(request)).hexdigest())
        if scope is None or project != scope.project_id:
            raise auth.AuthError('forbidden')
        return dict(principal_id=str(scope.principal_id), tenant_id=str(scope.tenant_id),
                    project_id=str(scope.project_id), permission_generation=scope.permission_generation)


def request(key=KEY_A, method='GET'):
    return dict(method=method, path='/search', query_string=b'q=source',
                headers=[(b'authorization', b'Bearer '+key), (b'x-project', b'p')],
                _c11b_body={'query': 'source'})


class SourceAdapters(unittest.IsolatedAsyncioTestCase):
    def private(self):
        try:
            return importlib.import_module('cortex_core.runtime.pg_adapters')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.runtime.pg_adapters':
                raise
            self.fail('SURFACE_ABSENT: private PG-D source adapters')

    def material(self, scope=SCOPE, credential=KEY_A):
        return self.private()._RequestMaterial(credential, scope)

    def authorized(self, connection):
        function = getattr(auth, '_async_authorized', None)
        if function is None:
            self.fail('SURFACE_ABSENT: private asynchronous C04')
        return function(connection, KEY_A, uid(1), uid(3), 'read')

    async def test_c04_local_role_command_order(self):
        for action in ('read', 'write', 'control'):
            connection = SyncConnection()
            with auth.authorized(connection, KEY_A, uid(1), uid(3), action) as scope:
                self.assertEqual(scope.action, action)
            queries = [r[0] for r in connection.events]
            self.assertIn(LOCAL_ROLE, queries, 'PGD_LOCAL_ROLE_MISSING')
            self.assertLess(queries.index(LOCAL_ROLE), queries.index(auth._ROLE_QUERY))
            role_query = next(r for r in connection.events if r[0] == auth._ROLE_QUERY)
            self.assertEqual(role_query[2], 'kaidera-runtime-core-request')
            self.assertEqual(connection.session_role, 'caller-selected-role')
            self.assertIsNone(connection.local_role)
            self.assertEqual([connection.context[n] for n in auth._CONTEXT], ['']*7)
        for foreign in ('closed', 'ambient'):
            connection = SyncConnection()
            if foreign == 'closed':connection.closed = True
            else:connection.autocommit = False
            with self.assertRaises(auth.AuthError):
                with auth.authorized(connection, KEY_A, uid(1), uid(3)):
                    self.fail('Ambient/closed connection admitted')
            self.assertEqual(connection.events, [], 'D02 existing refusal regression only')

    async def test_async_c04_context_clear_on_exit(self):
        self.authorized(Script().connection())  # distinguish an absent surface before starting a task
        for mode in ('success', 'error', 'cancel'):
            script = Script();connection = script.connection();entered = asyncio.Event()
            async def operation():
                async with self.authorized(connection) as scope:
                    self.assertEqual(scope, SCOPE)
                    connection.context.update(dict.fromkeys(auth._CONTEXT, 'poison'))
                    entered.set()
                    if mode == 'cancel':await asyncio.Event().wait()
                    if mode == 'error':raise RuntimeError('source body error')
                    # Restore the four admitted inputs so success can revalidate.
                    connection.context.update(zip(auth._CONTEXT[:4],
                        (hashlib.sha256(KEY_A).hexdigest(), str(uid(1)), str(uid(3)), 'read')))
            task = asyncio.create_task(operation());await asyncio.wait_for(entered.wait(), 2)
            if mode == 'cancel':task.cancel()
            if mode == 'success':await task
            else:
                with self.assertRaises(asyncio.CancelledError if mode == 'cancel' else RuntimeError):await task
            self.assertEqual(connection.clear_calls, 2, 'PGD_ASYNC_FINALLY_CLEAR_MISSING')
            self.assertEqual([connection.context[n] for n in auth._CONTEXT], ['']*7)
            self.assertFalse(connection.in_transaction)

    async def test_clear_failure_aborts_driver_and_withholds_result(self):
        script = Script();script.clear_error = asyncpg.InterfaceError('source clear failure')
        connection = script.connection();self.authorized(connection);emitted = [];error = None
        async def operation():
            async with self.authorized(connection):return 'private result'
        try:emitted.append(await operation())
        except Exception as caught:error = caught
        self.assertIsInstance(error, auth.AuthError, 'PGD_CLEAR_FAILURE_RESULT_ESCAPED')
        self.assertEqual(error.code, 'core_unavailable')
        self.assertEqual(emitted, [])
        self.assertTrue(connection.terminated, 'PGD_FAILED_CLEAR_DID_NOT_ABORT_DRIVER')
        self.assertEqual([r[0] for r in script.events].count('terminate'), 1)

    async def test_transaction_exit_error_has_no_result_or_redispatch(self):
        script = Script();script.commit_error = asyncpg.PostgresConnectionError('injected exit fault')
        connection = script.connection();self.authorized(connection);emitted = [];error = None
        try:
            async with self.authorized(connection):value = 'private'
            emitted.append(value)
        except Exception as caught:error = caught
        self.assertIsInstance(error, auth.AuthError, 'PGD_EXIT_FAULT_NOT_CLOSED_C04_ERROR')
        self.assertEqual(error.code, 'core_unavailable');self.assertEqual(emitted, [])
        self.assertEqual(sum(e[0] == 'begin' for e in script.events), 1)
        search = self.private()._C04Search(script, self.material())
        with self.assertRaises(CoreUnavailable):await search.search(str(uid(4)), IDENTITY, PROBE_VECTOR)
        self.assertEqual(script.borrowed, 1, 'Unknown exit must not redispatch')
        self.assertEqual(script.business, 1)
        # A confirmed rollback serialization is a different existing retry contract.
        script = Script();script.business_error = asyncpg.SerializationError('confirmed rollback')
        search = self.private()._C04Search(script, self.material())
        result = await search.search(str(uid(4)), IDENTITY, PROBE_VECTOR)
        self.assertEqual(len(result.hits), 1);self.assertEqual(script.business, 2)
        self.assertEqual(sum(e[0] == 'rollback' for e in script.events), 1)
        # Cleanup after known commit must not leak SerializationError into retry.
        script = Script();script.clear_error = asyncpg.SerializationError('clear after commit')
        search = self.private()._C04Search(script, self.material())
        with self.assertRaises(CoreUnavailable):await search.search(str(uid(4)), IDENTITY, PROBE_VECTOR)
        self.assertEqual(script.business, 1);self.assertEqual(script.borrowed, 1)

    async def test_c11_request_material_and_async_exit_revalidation(self):
        private = self.private();script = Script();record = RecordPort(script)
        port = C11b2ReadPort(record, script, IDENTITY, script, 'source-extractor', {'p': '/source'})
        scope = request(method='POST');principal = await port.principal(scope)
        response = await port.handlers['C01-R021'](principal, scope, 'request-key')
        self.assertTrue(response['complete']);self.assertEqual(response['query'], 'source')
        transactions = [c for c in script.connections if c.resolve_calls]
        self.assertTrue(transactions)
        self.assertTrue(all(c.resolve_calls == 2 for c in transactions), 'PGD_C11_C04_EXIT_REVALIDATION_MISSING')
        self.assertTrue(all(e[2][3] == 'read' for e in script.events if e[0] == 'fetchrow' and 'auth.resolve_scope' in e[1]))
        search, _ = port._ports(principal)
        self.assertIsInstance(search, private._C04Search)
        self.assertNotIn(KEY_A.decode(), repr(search._material))
        # The actual caller freezes original bytes rather than retaining request dicts.
        scope['headers'] = request(KEY_B)['headers']
        async with search._request(str(uid(4))) as (_, resolved):
            self.assertEqual(resolved.tenant_id, uid(2))
        self.assertEqual(script.connections[-1].scope.principal_id, uid(4))
        before = script.borrowed
        with self.assertRaises(PermissionError):
            async with search._request(str(uid(5))):pass
        self.assertEqual(script.borrowed, before)
        wrong = dict(principal, _request_scope=request())
        wrong['_request_scope']['headers'] = [(b'authorization', b'Bearer '+KEY_A), (b'x-project', b'other')]
        with self.assertRaises(auth.AuthError):port._ports(wrong)
        # Interleaved actual handlers on the same read port keep request material local.
        script = Script();port = C11b2ReadPort(RecordPort(script), script, IDENTITY, script, 'source-extractor', {'p': '/source'})
        entered, release = asyncio.Event(), asyncio.Event()
        async def provider_barrier():
            if script.provider_calls == 1:entered.set();await release.wait()
        script.on_embed = provider_barrier
        a, b = request(KEY_A, 'POST'), request(KEY_B)
        pa, pb = await port.principal(a), await port.principal(b)
        ta = asyncio.create_task(port.search(pa, a, 'a'));await asyncio.wait_for(entered.wait(), 2)
        rb = await port.search(pb, b, 'b');release.set();ra = await ta
        self.assertTrue(ra['complete'] and rb['complete']);self.assertEqual(script.provider_calls, 2)
        self.assertEqual({c.scope.principal_id for c in script.connections}, {uid(4), uid(5)})
        # Changed input context and six-field resolved scope refuse before yielding a result.
        for name in auth._CONTEXT[:4]:
            script = Script();connection = script.connection()
            with self.assertRaises(auth.AuthError):
                async with self.authorized(connection):connection.context[name] = 'changed'
        for field, changed in [('installation_id',uid(9)), ('tenant_id',uid(9)), ('project_id',uid(9)),
                               ('principal_id',uid(9)), ('permission_generation',8), ('action','write')]:
            script = Script();connection = script.connection()
            with self.assertRaises(auth.AuthError):
                async with self.authorized(connection):connection.exit_scope = replace(SCOPE, **{field: changed})
        script = Script();script.scopes[hashlib.sha256(KEY_A).hexdigest()] = None
        search = private._C04Search(script, self.material())
        with self.assertRaises(PermissionError):
            async with search._request(str(uid(4))):pass

    async def test_cache_full_partition_and_fresh_publish(self):
        private = self.private()
        canonical = json.dumps([str(uid(1)),str(uid(2)),str(uid(3)),str(uid(4)),7,'read'], separators=(',',':')).encode()
        self.assertEqual(private._cache_partition(SCOPE), hashlib.sha256(canonical).hexdigest())
        for field, changed in [('installation_id',uid(9)), ('tenant_id',uid(9)), ('project_id',uid(9)),
                               ('principal_id',uid(9)), ('permission_generation',8), ('action','write')]:
            self.assertNotEqual(private._cache_partition(SCOPE), private._cache_partition(replace(SCOPE, **{field:changed})), field)
        script = Script();cache = private._C04Cache(script, self.material(), script.embed)
        self.assertIs(type(cache)._publish, QueryEmbeddingCache._publish)
        cold = await cache.get(str(uid(4)), IDENTITY, 'same query')
        warm = await cache.get(str(uid(4)), IDENTITY, 'same query')
        self.assertFalse(cold.cache_hit);self.assertTrue(warm.cache_hit);self.assertEqual(script.provider_calls, 1)
        other = private._C04Cache(script, self.material(replace(SCOPE, principal_id=uid(5)), KEY_B), script.embed)
        self.assertFalse((await other.get(str(uid(5)), IDENTITY, 'same query')).cache_hit)
        script.scopes[hashlib.sha256(KEY_A).hexdigest()] = replace(SCOPE, permission_generation=8)
        current = private._C04Cache(script, self.material(replace(SCOPE, permission_generation=8)), script.embed)
        self.assertFalse((await current.get(str(uid(4)), IDENTITY, 'same query')).cache_hit)
        self.assertEqual(script.provider_calls, 3)
        script = Script()
        async def revoke():script.scopes[hashlib.sha256(KEY_A).hexdigest()] = replace(SCOPE, permission_generation=8)
        script.on_embed = revoke
        cache = private._C04Cache(script, self.material(), script.embed)
        with self.assertRaises(PermissionError):await cache.get(str(uid(4)), IDENTITY, 'fresh-publication')
        self.assertEqual(script.provider_calls, 1)
        self.assertFalse(any(e[0] == 'fetchval' and 'UPDATE retrieval.query_embeddings' in e[1] for e in script.events))
        self.assertEqual(script.active, 0)

    async def test_private_lease_calls_ttl_and_final_guard(self):
        private = self.private()
        script = Script();lease = private._LeasePorts(script, uid(1))
        self.assertEqual(await lease.acquire(), 42)
        await lease.renew()
        async def action(connection, fence):
            self.assertTrue(connection.in_transaction);self.assertEqual(fence, 42)
            return 'guarded result'
        self.assertEqual(await lease.guarded(action), 'guarded result')
        self.assertEqual((await lease.health())['state'], 'healthy')
        await lease.release();self.assertIsNone(lease.fence)
        queries = [e for e in script.events if e[0] in {'fetchval','fetchrow'}]
        for function in ('acquire','renew','release','assert_live','health'):
            self.assertTrue(any('coordination.runtime_lease_'+function+'(' in e[1] for e in queries), 'PGD_PRIVATE_PORT_MISSING '+function)
        self.assertFalse(any('coordination.supervisor_leases' in e[1] for e in queries))
        for event in queries:
            if 'runtime_lease_acquire' in event[1] or 'runtime_lease_renew' in event[1]:self.assertEqual(event[2][-1], 10000)
        with localcontext() as context:
            context.prec = 1
            for seconds, milliseconds in [(10,10000),(0.001,1),(0.1,100),(1.234,1234)]:
                self.assertEqual(private._ttl_ms(seconds), milliseconds)
        for invalid in [0, -1, 10.001, 0.0015, True, float('nan'), float('inf'), '1', Decimal('1'), 0.1+0.2]:
            with self.subTest(ttl=repr(invalid)):
                with self.assertRaises(ValueError):private._ttl_ms(invalid)
        script = Script();lease = private._LeasePorts(script, uid(1));await lease.acquire()
        async def expire(connection, fence):
            script.lease_live = False
            return 'must not escape'
        with self.assertRaises(StaleLease):await lease.guarded(expire)
        checks = [e for e in script.events if e[0] == 'fetchval' and 'runtime_lease_assert_live' in e[1]]
        self.assertEqual(len(checks), 2, 'PGD_FINAL_ASSERT_LIVE_MISSING')
        self.assertEqual(checks[0][3], checks[1][3]);self.assertTrue(all(e[4] for e in checks))
        self.assertEqual(script.events[-2][0], 'rollback')
        for value in (True, 0, -1, '42'):
            script = Script();script.lease_fence = value
            with self.assertRaises(CoreUnavailable):await private._LeasePorts(script, uid(1)).acquire()
        with self.assertRaises(CoreUnavailable):await lease.sample_metrics(None, None)

    async def test_observer_sample_mapping_and_fault_refusal(self):
        private = self.private();script = Script();observer = private._ObserverPorts(script, uid(1))
        self.assertEqual(await observer.read_sample(), CoreSample(4096, 2), 'PGD_OBSERVER_TOTAL_IS_NOT_EMBEDDING')
        self.assertEqual((await observer.read_health())['state'], 'healthy')
        calls = [e for e in script.events if e[0] in {'fetchval','fetchrow'}]
        self.assertEqual(sum('coordination.runtime_observe(' in e[1] for e in calls), 1)
        self.assertFalse(any('pg_database_size' in e[1] or 'FROM coordination.jobs' in e[1] for e in calls))
        for field in ('database_bytes','pending_jobs','pending_embedding_jobs'):
            for invalid in (True, -1, '2', None):
                script = Script();script.observe[field] = invalid
                with self.assertRaises(CoreUnavailable):await private._ObserverPorts(script, uid(1)).read_sample()
        script = Script();script.observe['extra'] = 0
        with self.assertRaises(CoreUnavailable):await private._ObserverPorts(script, uid(1)).read_sample()
        for error in (asyncpg.UndefinedFunctionError('dependency secret'), TimeoutError('aggregate failure')):
            script = Script();script.observe_error = error;observer = private._ObserverPorts(script, uid(1))
            with self.assertRaises(CoreUnavailable) as caught:await observer.read_sample()
            self.assertNotIn('dependency secret', str(caught.exception))
            self.assertEqual((await observer.read_health())['state'], 'healthy')

    async def test_read_committed_admission_and_single_statement_observation(self):
        private = self.private();script = Script();script.advancing = True
        search = private._C04Search(script, self.material())
        result = await search.search(str(uid(4)), IDENTITY, PROBE_VECTOR)
        self.assertEqual((result.freshness.indexed_records, result.freshness.pending_records, len(result.hits)), (1,0,1), 'PGD_SPLIT_SEARCH_OBSERVATION')
        async def recheck(subject, scope):return True
        target = {'record_id':uid(6), 'revision':2}
        observed = await search.projection_revision(str(uid(4)), target, IDENTITY, recheck=recheck)
        self.assertEqual(observed.state, 'visible', 'PGD_SPLIT_PROJECTION_OBSERVATION')
        data = [e for e in script.events if e[0] in {'fetch','fetchrow'} and ('retrieval.search_sources' in e[1] or 'FROM core.records' in e[1])]
        self.assertEqual(len(data), 2, 'Each returned observation must have one data statement')
        self.assertTrue(all(e[2]['isolation'] == 'read_committed' for e in script.events if e[0] == 'begin'))
        before = script.borrowed
        for isolation in ('repeatable_read','serializable',None):
            with self.assertRaises(ValueError):
                async with search._request(str(uid(4)), isolation=isolation):pass
        self.assertEqual(script.borrowed, before, 'PGD_INCOMPATIBLE_ISOLATION_BORROWED')
        script.empty = True;empty = await search.search(str(uid(4)), IDENTITY, PROBE_VECTOR)
        self.assertEqual((empty.hits, empty.freshness.indexed_records, empty.freshness.pending_records), ((),0,0))
        for override, expected_state, reason in [({'missing_core':True},'unavailable','target_unavailable'),
                ({'tombstone':True},'unavailable','tombstone'), ({'state':None,'state_identity':None},'unavailable','not_configured'),
                ({'state_identity':'other'},'unavailable','identity_mismatch'), ({'state':'rebuilding'},'unavailable','rebuilding'),
                ({'source_kind':'other'},'pending','index_pending'), ({'indexed_revision':1},'pending','index_pending')]:
            script.projection = override
            value = await search.projection_revision(str(uid(4)), target, IDENTITY, recheck=recheck)
            self.assertEqual((value.state,value.reason), (expected_state,reason))
        async def deny(subject, scope):return False
        with self.assertRaises(PermissionError):await search.projection_revision(str(uid(4)),target,IDENTITY,recheck=deny)


if __name__ == '__main__':
    unittest.main(verbosity=2)
