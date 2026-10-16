"""X4 deciding source boundaries. No SQL connection or native supplier is opened."""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.install import core_sql, lifecycle, owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import thaw
from cortex_core.runtime import role_grants
import test_core_sql as phases
import test_core_sql_seams as seams


class Boundaries(unittest.TestCase):
    def test_selected_manifest_and_full_ledger_join_original_member(self):
        helper=phases.CoreSQL()
        for damage in ('manifest','ledger','C09'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as root:
                s=helper.fixture(root);p=helper.primitive(s,'init');selected=copy.deepcopy(s.selection_data)
                if damage=='manifest':selected['manifest_sha256']='f'*64
                elif damage=='ledger':selected['migrations'][0][1]='e'*64
                else:selected['migrations'].append(['request-context-0009','d'*64]);selected['through']='request-context-0009'
                p.selection_ref=s.history.retain(canonical_bytes(selected))
                with s.installed(p),self.assertRaises(owner.OwnerRefusal):p._selection()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root);p=helper.primitive(s,'init')
            with s.installed(p):self.assertEqual(p._selection(),s.selection_data)

    def test_hold_preserves_exact_independent_unresolved_original(self):
        helper=phases.CoreSQL()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root);p=helper.primitive(s,'close')
            s.damage=lambda v:v['unresolved']['recovery'].append({'original_lifetime':'held-recovery-start'})
            c=json.loads(s.data['context_raw'])
            with patch.object(p,'_guard',return_value=c),s.installed(p),self.assertRaises(owner.OwnerRefusal):
                p._observed({'context':c},post=True)
            ref=getattr(p,'observation_ref',None)
            self.assertIsNotNone(ref,'X4_HOLD_INVENTORY_LOST')
            observed=json.loads(s.history.read(ref))
            self.assertEqual(observed['unresolved']['recovery'],[{'original_lifetime':'held-recovery-start'}])
            self.assertIsNone(s.history.head()[0])

    def test_root_initialization_requires_initdb_carrier_and_child_closed(self):
        helper=phases.CoreSQL()
        for kind in ('children','carriers'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as root:
                s=helper.fixture(root);p=helper.primitive(s,'init');c=json.loads(s.data['context_raw'])
                s.damage=lambda v:v['unresolved'][kind].append({'original_lifetime':'init-held'})
                with patch.object(p,'_guard',return_value=c),s.installed(p),self.assertRaises(owner.OwnerRefusal):
                    p._observed({'context':c},post=True)

    def test_each_sql_uses_same_finite_boundary_and_remaining_statement_cap(self):
        a=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter);trace=[];stop=1_500_000_000
        def boundary():trace.append('guard');return stop
        a._finite_boundary=boundary
        c=SimpleNamespace(execute=lambda *args,**kwargs:trace.append(args) or 'actual-result')
        self.assertTrue(callable(getattr(a,'_execute',None)),'X4_SQL_WINDOW_SEAM_ABSENT')
        with patch.object(role_grants.time,'monotonic_ns',return_value=1_000_000_000):
            self.assertEqual(a._execute(c,'SELECT actual_effect'),'actual-result')
        self.assertEqual(trace[0],'guard');self.assertEqual(trace[-1],'guard')
        self.assertEqual(trace[1][1],('500',));self.assertEqual(trace[2],('SELECT actual_effect',))
        trace.clear()
        with patch.object(role_grants.time,'monotonic_ns',return_value=stop),self.assertRaises(owner.OwnerRefusal):
            a._execute(c,'SELECT actual_effect')
        self.assertEqual(trace,['guard'])

    def test_finite_boundary_restored_after_dispatch_and_measure_failure(self):
        helper=phases.CoreSQL()
        with tempfile.TemporaryDirectory() as root:
            s=helper.fixture(root);p=helper.primitive(s,'init')
            self.assertTrue(callable(getattr(p,'_bounded',None)),'X4_FINITE_BOUNDARY_LIFETIME_ABSENT')
            client=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter)
            client._finite_boundary=None;inner=role_grants._Primitive.__new__(role_grants._Primitive);inner.client=client
            p.primitive=inner;c={}
            with self.assertRaisesRegex(RuntimeError,'lost'):
                with p._bounded(c):
                    self.assertTrue(callable(client._finite_boundary));raise RuntimeError('lost')
            self.assertIsNone(client._finite_boundary)

    def test_demote_changes_only_finite_roles_in_one_owned_transaction(self):
        a=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter);trace=[]
        a.roles={'migrator':'owned-migrator','bootstrap':'owned-bootstrap','api':'service-api'}
        import psycopg
        c=SimpleNamespace(execute=lambda sql,*args,**kwargs:trace.append(sql.as_string()))
        @contextmanager
        def transaction(conn):trace.append('begin');yield;trace.append('commit')
        with patch.object(a,'_transaction',transaction),patch.object(a,'_sql_capability',return_value=psycopg), \
             patch.object(a,'_current',side_effect=lambda *a,**k:trace.append('guard')),patch.object(a,'receipt',return_value='finite-data'):
            self.assertEqual(a.demote(c),'finite-data')
        writes=[x for x in trace if x.startswith('ALTER ROLE')]
        self.assertEqual(len(writes),2);self.assertIn('owned-migrator',writes[0]);self.assertIn('owned-bootstrap',writes[1])
        self.assertTrue(all('NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS' in x for x in writes))
        self.assertEqual(trace.count('begin'),1);self.assertEqual(trace.count('commit'),1)
        self.assertFalse(any('service-api' in x or 'terminate' in x for x in writes))

    def test_migration_checkpoint_scope_encloses_sql_before_owned_commit(self):
        a=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter);a._sql_transaction=None
        trace=[];c=seams.FakeConnection();helper=seams.MigrationBoundary()
        with tempfile.TemporaryDirectory() as root:
            path=helper.schema(root);manifest=json.loads((path/'manifest.json').read_bytes())
            params={'directory':path,'through':'one','manifest_sha256':hashlib.sha256((path/'manifest.json').read_bytes()).hexdigest(),
                    'ledger':[[r['id'],r['sha256']] for r in manifest['migrations']]}
            a._migration_selection=params
            @contextmanager
            def checkpoint(conn,transaction):
                self.assertIs(a._sql_transaction[0],conn);trace.append(('xid',len(c.queries),c.commits))
                yield
                trace.append(('postimage',len(c.queries),c.commits))
            with patch.object(a,'_mandated',return_value=True),patch.object(a,'_sql_transactional',checkpoint),patch.object(a,'_current'):
                self.assertEqual(a.migrate(c,**params),['one'])
            self.assertEqual(trace[0],('xid',0,0));self.assertEqual(trace[1][0],'postimage')
            self.assertGreater(trace[1][1],0);self.assertEqual(trace[1][2],0)
            self.assertEqual(c.commits,1);self.assertIsNone(a._sql_transaction)
