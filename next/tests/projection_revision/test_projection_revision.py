"""Pinned real C04/request-role controls; no provider or API wait machinery."""
import hashlib
import inspect
import sys
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'c11b2'))
import test_read_api as fixture
from cortex_core.auth import AuthError
from cortex_core.embeddings.pg_search import PostgresSearch, Scope
from cortex_core.modules.graph.pg_graph import Extraction, Source, encode_graph_fact


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    reset = fixture.ReadAPI.reset
    auth = fixture.ReadAPI.auth
    make_gateway = fixture.ReadAPI.make_gateway

    async def asyncSetUp(self):
        await fixture.ReadAPI.asyncSetUp(self)
        self.admin.execute("UPDATE core.records SET kind='knowledge' WHERE id=%s", (fixture.uid(6),))
        self.admin.execute("UPDATE retrieval.search_sources SET kind='knowledge' WHERE record_id=%s", (fixture.uid(6),))
        self.request_scope = {'method': 'GET', 'path': '/search',
            'headers': [(b'authorization', b'Bearer ' + fixture.READ_A), (b'x-project', b'3')],
            'query_string': b'q=fixture'}
        self.principal = await self.read_port.principal(self.request_scope)
        self.search, self.graph = self.read_port._ports(self.principal)
        self.rechecks = []

    async def asyncTearDown(self):
        await fixture.ReadAPI.asyncTearDown(self)

    async def current(self, subject, scope):
        self.assertEqual(subject, self.principal['principal_id'])
        self.assertEqual(str(scope.tenant_id), self.principal['tenant_id'])
        self.assertEqual(str(scope.project_id), self.principal['project_id'])
        await self.read_port._current(self.principal)
        self.rechecks.append(subject)
        return True

    async def observe(self, projection='search', *, record_id=None, revision=1, identity=None,
                      generation=None, recheck=None, port=None):
        adapter = port or (self.search if projection == 'search' else self.graph)
        method = getattr(adapter, 'projection_revision', None)
        self.assertTrue(callable(method), 'read-only projection revision port is missing')
        target = {'record_id': record_id or UUID(fixture.uid(6)), 'revision': revision}
        token = identity or fixture.IDENTITY if projection == 'search' else generation or self.generation
        return await method(self.principal['principal_id'], target, token, recheck=recheck or self.current)

    def advance(self, revision=2):
        with self.admin.transaction():
            self.admin.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES(%s,%s,%s,%s,%s,false)',
                               (*self.scope, fixture.uid(6), revision, fixture.uid(5)))
            self.admin.execute('UPDATE core.records SET current_revision=%s WHERE tenant_id=%s AND project_id=%s AND id=%s',
                               (revision, *self.scope, fixture.uid(6)))
            self.admin.execute('UPDATE retrieval.search_sources SET source_revision=%s WHERE tenant_id=%s AND project_id=%s AND record_id=%s',
                               (revision, *self.scope, fixture.uid(6)))

    def graph_receipt(self, revision=1, generation=None):
        generation = generation or self.generation
        source = Source(UUID(fixture.uid(6)), revision, 'knowledge', 'fixture concept', '', 'fixture content')
        facts = Extraction('fixture-extractor', (), ())
        body = encode_graph_fact(source, facts)
        payload, fact = uuid4(), uuid4()
        with self.admin.transaction():
            self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES(%s,%s,%s,%s,%s)',
                               (*self.scope, payload, body, hashlib.sha256(body).hexdigest()))
            self.admin.execute('INSERT INTO core.extraction_facts(tenant_id,project_id,id,record_id,source_revision,extractor_identity,payload_ref) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                               (*self.scope, fact, fixture.uid(6), revision, 'fixture-extractor', payload))
            self.admin.execute('INSERT INTO retrieval.graph_applied(tenant_id,project_id,generation,record_id,source_revision,source_kind,source_label,source_description,fact_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,project_id,generation,record_id) DO UPDATE SET source_revision=excluded.source_revision,fact_id=excluded.fact_id',
                               (*self.scope, generation, fixture.uid(6), revision, 'knowledge', 'fixture concept', '', fact))

    async def test_search_uses_indexed_not_desired_revision(self):
        self.advance()
        value = await self.observe(revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('pending', 1))
        self.assertEqual(value.identity, fixture.IDENTITY.key)
        self.admin.execute('UPDATE retrieval.search_vectors SET source_revision=2')
        value = await self.observe(revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('visible', 2))

    async def test_unrelated_record_cannot_supply_target_watermark(self):
        self.advance()
        other = UUID(int=5)
        self.admin.execute('INSERT INTO retrieval.search_sources VALUES(%s,%s,%s,%s,%s)',
                           (*self.scope, str(other), 'knowledge', 2))
        self.admin.execute('INSERT INTO retrieval.search_vectors VALUES(%s,%s,%s,%s,%s,%s::vector)',
                           (*self.scope, str(other), 2, fixture.IDENTITY.key,
                            '[' + ','.join(map(str, fixture.PROBE_VECTOR)) + ']'))
        value = await self.observe(revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('pending', 1))

    async def test_active_search_identity_is_mandatory(self):
        self.admin.execute("UPDATE retrieval.search_state SET identity='different-model'")
        value = await self.observe()
        self.assertEqual(value.state, 'unavailable')
        self.assertIsNone(value.indexed_revision)

    async def test_graph_pending_then_empty_fact_receipt_visible(self):
        value = await self.observe('graph')
        self.assertEqual(value.state, 'pending')
        self.graph_receipt()
        value = await self.observe('graph')
        self.assertEqual((value.state, value.indexed_revision), ('visible', 1))
        self.assertEqual(value.generation, self.generation)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM retrieval.graph_nodes').fetchone()[0], 0)

    async def test_graph_old_revision_does_not_cover_new_target(self):
        self.graph_receipt()
        self.advance()
        value = await self.observe('graph', revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('pending', 1))
        self.graph_receipt(2)
        value = await self.observe('graph', revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('visible', 2))

    async def test_graph_generation_and_extractor_identity_are_bound(self):
        self.graph_receipt()
        value = await self.observe('graph', generation=uuid4())
        self.assertEqual(value.state, 'unavailable')
        self.assertIsNone(value.indexed_revision)
        self.admin.execute("UPDATE retrieval.graph_generations SET extractor_identity='different-extractor'")
        value = await self.observe('graph')
        self.assertEqual(value.state, 'unavailable')

    async def test_tombstone_has_no_invented_indexed_delete_receipt(self):
        self.graph_receipt()
        self.admin.execute('UPDATE core.records SET tombstone=true WHERE id=%s', (fixture.uid(6),))
        for projection in ('search', 'graph'):
            with self.subTest(projection=projection):
                value = await self.observe(projection)
                self.assertEqual(value.state, 'unavailable')
                self.assertIsNone(value.indexed_revision)

    async def test_other_tenant_target_and_unknown_are_indistinguishable(self):
        other = UUID(fixture.uid(21))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES(%s,%s,%s,'knowledge',1,false)",
                               (fixture.uid(21), fixture.uid(3), other))
            self.admin.execute('INSERT INTO core.record_revisions(tenant_id,project_id,record_id,revision,payload_ref,tombstone) VALUES(%s,%s,%s,1,%s,false)',
                               (fixture.uid(21), fixture.uid(3), other, fixture.uid(5)))
        for projection in ('search', 'graph'):
            with self.subTest(projection=projection):
                foreign = await self.observe(projection, record_id=other)
                unknown = await self.observe(projection, record_id=uuid4())
                self.assertEqual((foreign.state, foreign.reason, foreign.indexed_revision),
                                 (unknown.state, unknown.reason, unknown.indexed_revision))
                self.assertEqual(foreign.state, 'unavailable')

    async def test_recheck_is_required_explicit_and_rejects_false(self):
        method = getattr(self.search, 'projection_revision', None)
        self.assertTrue(callable(method))
        self.assertIs(inspect.signature(method).parameters['recheck'].default, inspect.Parameter.empty)
        async def refuse(subject, scope):
            return False
        with self.assertRaises(PermissionError):
            await self.observe(recheck=refuse)

    async def test_final_real_core_recheck_stops_late_revocation(self):
        async def revoke(subject, scope):
            self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                               (hashlib.sha256(fixture.READ_A).hexdigest(),))
            return await self.current(subject, scope)
        with self.assertRaises(AuthError):
            await self.observe(recheck=revoke)

    async def test_forged_and_revoked_credentials_cannot_observe(self):
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',
                           (hashlib.sha256(fixture.READ_A).hexdigest(),))
        method = getattr(self.search, 'projection_revision', None)
        self.assertTrue(callable(method))
        with self.assertRaises(AuthError):
            await self.observe()
        forged = {**self.request_scope, 'headers': [(b'authorization', b'Bearer synthetic-forged'), (b'x-project', b'3')]}
        with self.assertRaises(AuthError):
            await self.read_port.principal(forged)

    async def test_disabled_rebuilding_and_missing_projection_are_unavailable(self):
        for state in ('disabled', 'rebuilding'):
            self.admin.execute('UPDATE retrieval.search_state SET state=%s', (state,))
            value = await self.observe()
            self.assertEqual(value.state, 'unavailable')
        self.admin.execute('DELETE FROM retrieval.search_state')
        value = await self.observe()
        self.assertEqual(value.state, 'unavailable')

    async def test_rollback_never_advances_observed_graph_revision(self):
        self.graph_receipt()
        self.advance()
        with self.admin.transaction(force_rollback=True):
            self.graph_receipt(2)
        value = await self.observe('graph', revision=2)
        self.assertEqual((value.state, value.indexed_revision), ('pending', 1))

    async def test_observation_is_data_readonly_and_checks_current_authority_once(self):
        before = self.admin.execute('SELECT count(*),sum(source_revision) FROM retrieval.search_vectors').fetchone()
        value = await self.observe()
        self.assertEqual(value.state, 'visible')
        self.assertEqual(self.rechecks, [self.principal['principal_id']])
        self.assertEqual(self.admin.execute('SELECT count(*),sum(source_revision) FROM retrieval.search_vectors').fetchone(), before)

    async def test_invalid_target_cannot_be_a_global_or_boolean_cursor(self):
        method = getattr(self.search, 'projection_revision', None)
        self.assertTrue(callable(method))
        for target in ({'record_id': UUID(fixture.uid(6)), 'revision': True},
                       {'record_id': UUID(fixture.uid(6)), 'revision': 0},
                       {'min_revision': 2}, {'record_id': 'unbound', 'revision': 1}):
            with self.subTest(target_shape=list(target)):
                with self.assertRaises(ValueError):
                    await method(self.principal['principal_id'], target, fixture.IDENTITY, recheck=self.current)

    async def test_plain_guc_scope_without_private_core_binding_cannot_observe(self):
        async def unbound(conn, subject):
            await conn.execute("SELECT set_config('cortex.tenant_id',$1,true),set_config('cortex.project_id',$2,true)",
                               self.principal['tenant_id'], self.principal['project_id'])
            return Scope(UUID(self.principal['tenant_id']), UUID(self.principal['project_id']))
        # Deliberately invalid test callback proves real C04 restrictive RLS,
        # never a production fallback or positive authority fixture.
        port = PostgresSearch(self.pool, unbound)
        value = await self.observe(port=port)
        self.assertEqual(value.state, 'unavailable')
        self.assertIsNone(value.indexed_revision)

    async def test_core_down_has_no_false_visibility(self):
        method = getattr(self.search, 'projection_revision', None)
        self.assertTrue(callable(method))
        await self.pool.close()
        value = await self.observe()
        self.assertEqual((value.state, value.reason, value.indexed_revision),
                         ('unavailable', 'core_unavailable', None))


if __name__ == '__main__':
    unittest.main()
