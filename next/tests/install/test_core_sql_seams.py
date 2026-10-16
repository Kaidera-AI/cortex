"""X4 transaction/finite-phase seams with driver enums and fake SQL only."""
from contextlib import contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from psycopg.pq import TransactionStatus as TS
from cortex_core import migrations
from cortex_core.install import lifecycle, owner
from cortex_core.runtime import role_grants


class FakeConnection:
    def __init__(self):
        self.autocommit=True;self.info=SimpleNamespace(transaction_status=TS.IDLE)
        self.rows=[];self.commits=0;self.queries=[];self.pending=[]
    @contextmanager
    def transaction(self):
        self.info.transaction_status=TS.INTRANS;self.pending=list(self.rows)
        try:
            yield SimpleNamespace(savepoint_name='')
        except BaseException:
            self.pending=[];raise
        else:self.rows=self.pending;self.commits+=1
        finally:self.info.transaction_status=TS.IDLE
    def execute(self,sql,args=(),**kwargs):
        self.queries.append(sql)
        if sql.startswith('INSERT INTO core.schema_migrations'):self.pending.append(tuple(args))
        return SimpleNamespace(fetchall=lambda:list(self.pending),fetchone=lambda:(True,))


class MigrationBoundary(unittest.TestCase):
    def schema(self,root):
        p=Path(root);raw=b'SELECT 1;';(p/'one.sql').write_bytes(raw)
        (p/'manifest.json').write_text(json.dumps({'version':1,'migrations':[
            {'id':'one','file':'one.sql','sha256':hashlib.sha256(raw).hexdigest()}]}))
        return p
    def run_migration(self,c,path,guard):
        self.assertIn('_boundary',inspect.signature(migrations.apply_migrations).parameters,
                      'X4_MIGRATION_BOUNDARY_ABSENT')
        return migrations.apply_migrations(c,path,through='one',_boundary=guard)
    def test_original_budget_expiry_before_commit_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            c=FakeConnection();seen=[]
            def guard(stage,conn):
                seen.append(stage)
                if stage=='before_commit':raise owner.OwnerRefusal('reservation_expired')
            with self.assertRaises(owner.OwnerRefusal):self.run_migration(c,self.schema(root),guard)
            self.assertIn('before_commit',seen);self.assertEqual(c.commits,0);self.assertEqual(c.rows,[])
    def test_guard_covers_each_sql_and_commit_and_reentry(self):
        with tempfile.TemporaryDirectory() as root:
            c=FakeConnection();seen=[];path=self.schema(root)
            self.assertEqual(self.run_migration(c,path,lambda stage,conn:seen.append(stage)),['one'])
            self.assertEqual(seen.count('before_sql'),len(c.queries));self.assertEqual(seen.count('after_sql'),len(c.queries))
            self.assertEqual(seen[-2:],['before_commit','after_commit'])
            self.assertEqual(self.run_migration(c,path,lambda *a:None),[]);self.assertEqual(len(c.rows),1)
    def test_after_commit_expiry_does_not_claim_rollback_or_retry(self):
        with tempfile.TemporaryDirectory() as root:
            c=FakeConnection()
            def guard(stage,conn):
                if stage=='after_commit':raise owner.OwnerRefusal('reservation_expired')
            with self.assertRaises(owner.OwnerRefusal):self.run_migration(c,self.schema(root),guard)
            self.assertEqual(c.commits,1);self.assertEqual(len(c.rows),1)


class FiniteRolePhase(unittest.TestCase):
    def adapter(self):
        a=role_grants._RoleAdapter.__new__(role_grants._RoleAdapter)
        a.connection=object();a.recovery=object();a.roles={'bootstrap':'bootstrap','migrator':'migrator'}
        a.database=(10,'owned',20);a.context={'context':{'identity':{'installation_id':'install','operation_id':'op'}}}
        return a
    def test_exact_migrator_private_phase_and_oid_admitted(self):
        a=self.adapter();c=SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchone=lambda:(22,22,'migrator','migrator')))
        a.connection=c
        original={'database':b'{"database":[10,"owned",20]}'}
        private={'phase':'migrator','session_user_name':'migrator','session_user_oid':22}
        with patch.object(a,'_sql_capability'),patch.object(a,'_current',return_value=original), \
             patch.object(a,'_database',return_value=a.database),patch.object(lifecycle,'_owner_inputs',return_value=object()), \
             patch.object(owner,'_private',return_value=private):
            try:a.admit(c)
            except owner.OwnerRefusal as error:self.fail('exact finite migrator refused: '+error.detail)
    def test_runtime_private_phase_cannot_borrow_bootstrap(self):
        a=self.adapter();c=SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchone=lambda:(20,20,'bootstrap','bootstrap')))
        a.connection=c;original={'database':b'{"database":[10,"owned",20]}'}
        private={'phase':'runtime','session_user_name':'bootstrap','session_user_oid':20}
        with patch.object(a,'_sql_capability'),patch.object(a,'_current',return_value=original), \
             patch.object(a,'_database',return_value=a.database),patch.object(lifecycle,'_owner_inputs',return_value=object()), \
             patch.object(owner,'_private',return_value=private),self.assertRaises(owner.OwnerRefusal):a.admit(c)
    def test_demotion_separate_from_owner_final_socket_absence(self):
        self.assertTrue(callable(getattr(role_grants._RoleAdapter,'demote',None)),
                        'X4_FINAL_CLOSE_SPLIT_ABSENT')
    def test_migration_reuses_existing_transaction_checkpoint_scope(self):
        self.assertTrue(callable(getattr(role_grants._RoleAdapter,'migrate',None)),
                        'X4_MIGRATION_CHECKPOINT_SEAM_ABSENT')
