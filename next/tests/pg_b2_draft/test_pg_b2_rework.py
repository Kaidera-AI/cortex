"""Owned SQL capability/ledger regressions; source recorders, no PG effects."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import tempfile
import unittest
from uuid import UUID
from psycopg.pq import TransactionStatus
from cortex_core.install import owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime import role_grants as api
from pgb2_preimages import PGPreimageFixture
import test_hale_review as hale

class Connection(hale.RecordingConnection):
    def __init__(self,**kwargs):super().__init__(**kwargs);self.events=[]
    def execute(self,statement,parameters=None):
        text=statement if isinstance(statement,str) else statement.as_string()
        if text.startswith('SELECT oid,datname,datdba'):
            self.calls.append((text,parameters));return hale.Rows([(55,'hale_source_db',hale.OIDS[hale.ROLES['bootstrap']],True,-1)])
        if text.startswith('SELECT a.usesysid'):
            self.calls.append((text,parameters));oid=hale.OIDS[hale.ROLES['bootstrap']];return hale.Rows([(oid,oid,hale.ROLES['bootstrap'],hale.ROLES['bootstrap'])])
        if text.startswith('SELECT to_regclass'):
            self.calls.append((text,parameters));return hale.Rows([(False,)])
        return super().execute(statement,parameters)
    @contextmanager
    def transaction(self):
        previous=self.info.transaction_status;outer=previous==TransactionStatus.IDLE
        self.contexts.append('BEGIN_COMMIT' if outer else 'SAVEPOINT_RELEASE');self.events.append('BEGIN' if outer else 'SAVEPOINT')
        self.info.transaction_status=TransactionStatus.INTRANS
        try:yield SimpleNamespace(savepoint_name=None if outer else '_pg3_1')
        except BaseException:self.events.append('ROLLBACK' if outer else 'ROLLBACK_SAVEPOINT');raise
        else:self.events.append('COMMIT' if outer else 'RELEASE')
        finally:self.info.transaction_status=previous

class SQLCapability(unittest.TestCase):
    def arguments(self,connection,recovery):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup);f=PGPreimageFixture(self,root.name)
        return dict(history=f.history,identity=f.identity,connection=connection,recovery_connection=recovery,
            role_names=dict(hale.ROLES),installation_id=UUID(f.identity['installation_id']),embedding_job_kind='fixture',
            policy=hale.MATRIX,materials=SimpleNamespace(snapshot=lambda:{'hba':b'h','carrier':b'c'},restore=lambda values:None),backends={})
    def adapter(self,connection=None,recovery=None):
        conn=connection if connection is not None else Connection();root=recovery if recovery is not None else Connection()
        a=api._RoleAdapter(**self.arguments(conn,root))
        original={'database':list(a.database),'bindings':None}
        a._current=lambda *args:{'database':canonical_bytes(original),'role':canonical_bytes({'roles':[],'memberships':[]})}
        return a,conn,root
    def refused_ctor(self,*,finite=True,autocommit=True,status=TransactionStatus.IDLE):
        conn=Connection();root=Connection();bad=conn if finite else root;bad.autocommit=autocommit;bad.info.transaction_status=status
        try:api._RoleAdapter(**self.arguments(conn,root))
        except owner.OwnerRefusal as error:self.assertEqual(error.detail,'atomicity_unavailable')
        else:self.fail('PG_B2_UNOWNED_CTOR_CATALOGUE_READ_ADMITTED')
        self.assertEqual(conn.calls+root.calls,[])
    def test_finite_non_autocommit_refused_before_constructor_read(self):self.refused_ctor(autocommit=False)
    def test_finite_ambient_refused_before_constructor_read(self):self.refused_ctor(status=TransactionStatus.INTRANS)
    def test_recovery_non_autocommit_refused_before_constructor_read(self):self.refused_ctor(finite=False,autocommit=False)
    def test_recovery_ambient_refused_before_constructor_read(self):self.refused_ctor(finite=False,status=TransactionStatus.INTRANS)
    def test_other_transaction_statuses_refused_before_constructor_read(self):
        for status in (TransactionStatus.ACTIVE,TransactionStatus.INERROR,TransactionStatus.UNKNOWN):
            with self.subTest(status=status):self.refused_ctor(status=status)
    def test_idle_autocommit_constructor_control(self):
        a,conn,root=self.adapter();self.assertEqual(conn.calls,[]);self.assertEqual(len(root.calls),1)
    def test_admission_rechecks_non_autocommit_before_read(self):
        a,conn,root=self.adapter();conn.autocommit=False;conn.calls.clear()
        try:a.admit(conn)
        except owner.OwnerRefusal:return self.assertEqual(conn.calls,[])
        self.fail('PG_B2_REUSED_ADMISSION_NON_AUTOCOMMIT_ADMITTED')
    def test_admission_rechecks_ambient_before_read(self):
        a,conn,root=self.adapter();conn.info.transaction_status=TransactionStatus.INTRANS;conn.calls.clear()
        try:a.admit(conn)
        except owner.OwnerRefusal:return self.assertEqual(conn.calls,[])
        self.fail('PG_B2_REUSED_ADMISSION_AMBIENT_ADMITTED')
    def test_snapshot_rechecks_capability_before_material_or_catalogue_read(self):
        a,conn,root=self.adapter();a._roles=lambda c:{'roles':[],'memberships':[]};a._acl=lambda c:{'objects':[],'defaults':[]}
        reads=[];a.materials.snapshot=lambda:reads.append('material') or {'hba':b'h','carrier':b'c'}
        root.autocommit=False;root.calls.clear()
        try:a.snapshot()
        except owner.OwnerRefusal:self.assertEqual(reads+root.calls,[]);return
        self.fail('PG_B2_UNOWNED_SNAPSHOT_READ_ADMITTED')
    def test_internal_snapshot_rechecks_ambient_before_reads(self):
        a,conn,root=self.adapter();a._roles=lambda c:{'roles':[],'memberships':[]};a._acl=lambda c:{'objects':[],'defaults':[]}
        reads=[];a.materials.snapshot=lambda:reads.append('material') or {'hba':b'h','carrier':b'c'}
        root.info.transaction_status=TransactionStatus.INTRANS;root.calls.clear()
        try:a._snapshot(root)
        except owner.OwnerRefusal:self.assertEqual(reads+root.calls,[]);return
        self.fail('PG_B2_AMBIENT_INTERNAL_SNAPSHOT_ADMITTED')
    def test_top_level_commit_precedes_independent_measurement(self):
        conn=Connection();a=hale.isolated_adapter(conn)
        with a._transaction(conn):conn.events.append('effect')
        conn.events.append('independent_measurement')
        self.assertEqual(conn.events,['BEGIN','effect','COMMIT','independent_measurement'])
        self.assertEqual(conn.info.transaction_status,TransactionStatus.IDLE)
    def test_reuse_after_owned_commit_rejects_new_outer_transaction(self):
        conn=Connection();a=hale.isolated_adapter(conn)
        with a._transaction(conn):pass
        conn.calls.clear();conn.info.transaction_status=TransactionStatus.INTRANS
        try:
            with a._transaction(conn):self.fail('PG_B2_AMBIENT_REUSE_ENTERED_BODY')
        except owner.OwnerRefusal:self.assertEqual(conn.calls,[]);return
        self.fail('PG_B2_AMBIENT_REUSE_ADMITTED')
    def test_failure_rolls_back_and_does_not_leave_owned_scope(self):
        conn=Connection();a=hale.isolated_adapter(conn)
        with self.assertRaisesRegex(RuntimeError,'fixture abort'):
            with a._transaction(conn):raise RuntimeError('fixture abort')
        self.assertEqual(conn.events,['BEGIN','ROLLBACK'])
        conn.info.transaction_status=TransactionStatus.INTRANS
        with self.assertRaises(owner.OwnerRefusal):
            with a._transaction(conn):self.fail('leaked scope')
    def test_recovery_ambient_refused_before_finite_admission_read(self):
        a,conn,root=self.adapter();root.info.transaction_status=TransactionStatus.INTRANS;conn.calls.clear();root.calls.clear()
        try:a.admit(conn)
        except owner.OwnerRefusal:self.assertEqual(conn.calls+root.calls,[]);return
        self.fail('PG_B2_AMBIENT_RECOVERY_COMPANION_ADMITTED')
    def test_recovery_non_autocommit_refused_before_finite_transaction(self):
        conn=Connection();a=hale.isolated_adapter(conn);a.recovery.autocommit=False
        with self.assertRaises(owner.OwnerRefusal):
            with a._transaction(conn):self.fail('unowned recovery entered finite body')
        self.assertEqual(conn.calls,[])
    def test_independent_snapshot_cannot_run_before_owned_commit(self):
        a,conn,root=self.adapter();a._roles=lambda c:{'roles':[],'memberships':[]};a._acl=lambda c:{'objects':[],'defaults':[]}
        with a._transaction(conn):
            with self.assertRaises(owner.OwnerRefusal):a.snapshot()
    def test_recovery_snapshot_after_finite_socket_closed_is_allowed(self):
        a,conn,root=self.adapter();a._roles=lambda c:{'roles':[],'memberships':[]};a._acl=lambda c:{'objects':[],'defaults':[]}
        conn.closed=True;conn.info.transaction_status=TransactionStatus.UNKNOWN
        self.assertEqual(set(a.snapshot()),{'role','acl','database','hba','carrier'})
    def test_foreign_connection_refused_before_sql_read(self):
        a,conn,root=self.adapter();foreign=Connection()
        with self.assertRaises(owner.OwnerRefusal):a.admit(foreign)
        self.assertEqual(foreign.calls,[])

class Catalogue(hale.ACLCatalogue):
    def __init__(self,a,*,logical=None,grant_option=False,ownership=False):
        super().__init__(a);self.grantable=set()
        if logical:
            self.truth.add((hale.ROLES[logical],'TABLE',self.ledger,'SELECT'))
            for row in self.acl['objects']:
                if row[4]==self.ledger:
                    row[3].append(hale.ROLES[logical]+'=r/'+hale.ROLES['migrator'])
                    if ownership:row[2]=hale.OIDS[hale.ROLES[logical]]
            if grant_option or ownership:self.grantable.add((hale.ROLES[logical],'TABLE',self.ledger,'SELECT'))
    def execute(self,statement,parameters=None):
        text=statement if isinstance(statement,str) else statement.as_string()
        if 'WITH ORDINALITY AS q(' in text:
            self.calls.append((text,parameters));roles,kinds,names,privileges=parameters
            return hale.Rows([(i,(r,k,n,p) in self.truth,(r,k,n,p) in self.grantable) for i,(r,k,n,p) in enumerate(zip(roles,kinds,names,privileges),1)])
        if text.startswith('REVOKE '):self.calls.append((text,parameters));return hale.Rows()
        return super().execute(statement,parameters)

class LedgerDenials(unittest.TestCase):
    def test_explicit_denied_principals_have_zero_ledger_plan(self):
        a=hale.acl_adapter();plans=a._plans();expected={*api._SERVICES,'request_group','verifier_owner'}
        rows=[r for r in plans if r[1:3]==('TABLE',hale.MATRIX['control_ledger']['name'])]
        self.assertEqual({r[0] for r in rows},expected);self.assertTrue(all(r[3]==[] for r in rows))
    def test_each_denied_principal_extra_select_is_refused(self):
        for logical in (*api._SERVICES,'request_group','verifier_owner'):
            with self.subTest(logical=logical):
                a=hale.acl_adapter();conn=Catalogue(a,logical=logical)
                with self.assertRaises(owner.OwnerRefusal):a.validate_acl(conn)
    def test_ledger_grant_option_is_refused(self):
        a=hale.acl_adapter();conn=Catalogue(a,logical='api_request',grant_option=True)
        with self.assertRaises(owner.OwnerRefusal):a.validate_acl(conn)
    def test_revocable_grant_emits_revoke_through_existing_plan(self):
        a=hale.acl_adapter();conn=Catalogue(a,logical='probe',grant_option=True);a._apply_grants(conn,a._plans())
        statements=[t for t,p in conn.calls if t.startswith('REVOKE ')]
        self.assertTrue(any('REVOKE SELECT ON TABLE core.schema_migrations FROM' in t and hale.ROLES['probe'] in t for t in statements))
        self.assertTrue(any('REVOKE GRANT OPTION FOR SELECT ON TABLE core.schema_migrations' in t for t in statements))
    def test_ownership_derived_rights_remain_refusal_after_revoke(self):
        a=hale.acl_adapter();conn=Catalogue(a,logical='verifier_owner',ownership=True);a._apply_grants(conn,a._plans())
        with self.assertRaises(owner.OwnerRefusal):a.validate_acl(conn)
