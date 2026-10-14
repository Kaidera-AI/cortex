"""Separate supplemental PG-B direct-draft controls. Never frozen181 credit.
Original test methods are reused verbatim with a synthetic test-controller
binding/ACL setup; no finite production-owner or installed authority is proven.
"""
import sys
from pathlib import Path
from uuid import UUID
import psycopg
from psycopg import sql
NEXT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'tests/runtime_grants_draft'))
from test_draft import DraftFixture, Draft as PriorDraft
import test_ports
import test_repair
from support import MATRIX,ROLE_NAMES,OPERATION,EMBEDDING_KIND,uid

class PgBDraftFixture(DraftFixture):
    def candidate_if_present(self):
        if self._candidate:return
        super().candidate_if_present()
        # Owned test-only inputs. This does not attest a production producer.
        self.root.execute('UPDATE auth.runtime_login_bindings SET embedding_job_kind=%s WHERE logical_role=%s',(EMBEDDING_KIND,'observer'))
        for row in MATRIX['functions']:
            if row.get('proposed'):
                for logical in ('supervisor','observer'):
                    if row['direct_privileges'].get(logical)==['EXECUTE']:
                        self.root.execute(sql.SQL('GRANT EXECUTE ON FUNCTION '+row['signature']+' TO {}').format(sql.Identifier(ROLE_NAMES[logical])))

    def bind_current(self,*,embedding_kind=EMBEDDING_KIND,operation=OPERATION):
        self.candidate_if_present()
        if operation!=OPERATION:raise ValueError('synthetic fixture operation differs')
        if embedding_kind is None:raise ValueError('synthetic fixture embedding input missing')
        before=self.root.execute('SELECT embedding_job_kind FROM auth.runtime_login_bindings WHERE logical_role=%s',('observer',)).fetchone()[0]
        if before!=embedding_kind:raise ValueError('synthetic fixture embedding input differs')
        return {'scope':'synthetic fixture only'}

class Ports(PgBDraftFixture,test_ports.Ports):pass
class Prior(PgBDraftFixture,PriorDraft):pass

class Repair(PgBDraftFixture,test_repair.Repair):
    pass
# Reuse only actual port assertions, not fixture-independent lifecycle/ledger
# assertions that require the separately gated finite client/final manifest.
PORT_REPAIR_NAMES={
 'test_observer_final_revision_only_barrier','test_health_final_revision_only_barrier',
 'test_health_revoked_map_refuses_at_entry','test_expired_matching_holder_release_has_no_effect',
 'test_assert_live_itself_holds_row_before_any_later_write','test_one_ms_acquire_is_valid_without_sleep_dependent_health',
 'test_supervisor_own_session_admits_positive_lease','test_valid_bound_observer_aggregate_error_never_success_zero',
 'test_valid_bound_observer_timeout_never_success_zero'}
for name in dir(test_repair.Repair):
    if name.startswith('test_') and (name in PORT_REPAIR_NAMES or name.startswith('test_snapshot_') or name.startswith('test_map_') or name.startswith('test_queued_')):
        continue
    if name.startswith('test_'):setattr(Repair,name,None)

class Boundary(PgBDraftFixture):
    def test_private_port_matrix_and_public_grant_options_closed(self):
        self.candidate_if_present()
        count=self.root.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('core','auth','coordination','retrieval')").fetchone()[0]
        self.assertEqual(count,len(MATRIX['functions']))
        self.assertEqual(count,73)
        self.assertEqual(self.root.execute('SELECT count(*) FROM core.schema_migrations').fetchone(),(19,))
        for row in MATRIX['functions']:
            if not row['signature'].startswith('coordination.runtime_'):continue
            signature=row['signature']
            public=self.root.execute("SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=%s::regprocedure AND a.grantee=0",(signature,)).fetchone()[0]
            self.assertEqual(public,0,'PUBLIC new private port:'+signature)
            for logical in ('request_group','api_request','probe','supervisor','observer','installer_enrollment'):
                want='EXECUTE' in row['direct_privileges'][logical]
                actual=self.root.execute("SELECT has_function_privilege(%s,%s,'EXECUTE'),has_function_privilege(%s,%s,'EXECUTE WITH GRANT OPTION')",(ROLE_NAMES[logical],signature,ROLE_NAMES[logical],signature)).fetchone()
                self.assertEqual(actual,(want,False),'private ACL:'+logical+':'+signature)
    def test_insert_work_does_not_spend_valid_minimum_ttl(self):
        self.candidate_if_present()
        # Owned fixture latency, not a product source mutation or new authority.
        self.root.execute("CREATE FUNCTION public.pgb_insert_delay() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(.02); RETURN NEW; END; $$")
        self.root.execute('CREATE TRIGGER pgb_insert_delay AFTER INSERT ON coordination.supervisor_leases FOR EACH ROW EXECUTE FUNCTION public.pgb_insert_delay()')
        conn=self.login('supervisor')
        try:value=conn.execute('SELECT coordination.runtime_lease_acquire(%s,%s,1)',(uid(1),uid(100))).fetchone()[0]
        except psycopg.errors.ObjectNotInPrerequisiteState:
            self.fail('SEMANTIC:prepublication processing consumed valid minimum TTL')
        self.assertTrue(type(value) is int and value>0)
    def test_minimum_ttl_acquires_positive_without_prepublication_expiry(self):
        self.candidate_if_present()
        conn=self.login('supervisor')
        try:value=conn.execute('SELECT coordination.runtime_lease_acquire(%s,%s,1)',(uid(1),uid(100))).fetchone()[0]
        except psycopg.errors.ObjectNotInPrerequisiteState:
            self.fail('SEMANTIC:valid 1ms acquire denied before publication')
        self.assertTrue(type(value) is int and value>0)
    def test_observer_unbound_kind_refuses_without_zero(self):
        self.candidate_if_present()
        self.root.execute("UPDATE auth.runtime_login_bindings SET embedding_job_kind=NULL WHERE logical_role='observer'")
        self.refusal(self.login('observer'),'SELECT coordination.runtime_observe(%s)',(uid(1),))
    def test_supervisor_no_raw_map_or_lease_privileges_and_observer_no_mutation(self):
        self.candidate_if_present()
        for logical in ('supervisor','observer'):
            conn=self.login(logical)
            for table in ('auth.runtime_login_bindings','coordination.supervisor_leases'):
                self.refusal(conn,'SELECT count(*) FROM '+table)
        self.refusal(self.login('observer'),'SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(1),uid(100)))
    def test_write_map_wait_revision_change_refuses_before_lease_effect(self):
        self.candidate_if_present()
        locker=self.login('bootstrap');locker.execute('BEGIN')
        locker.execute('SELECT role_oid FROM auth.runtime_login_bindings WHERE logical_role=%s FOR UPDATE',('supervisor',))
        _,pid,box,thread=self.background('supervisor','SELECT coordination.runtime_lease_acquire(%s,%s,1000)',(uid(1),uid(100)))
        try:
            self.wait_blocked(pid,locker.info.backend_pid,box)
            locker.execute('UPDATE auth.runtime_login_bindings SET revision=revision+1 WHERE logical_role=%s',('supervisor',))
        finally:locker.execute('COMMIT')
        self.joined(thread,box)
        self.assertIn('error',box,'stale queued revision admitted')
        error=box['error']
        if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):raise error
        self.assertEqual(self.root.execute('SELECT count(*) FROM coordination.supervisor_leases').fetchone(),(0,))
    def test_read_operation_only_change_refuses_publication(self):
        self.candidate_if_present()
        locker=self.login('bootstrap');locker.execute('BEGIN');locker.execute('LOCK TABLE coordination.supervisor_leases IN ACCESS EXCLUSIVE MODE')
        observer,pid,box,thread=self.background('observer','SELECT coordination.runtime_lease_health(%s)',(uid(1),),readonly=True)
        try:
            self.wait_blocked(pid,locker.info.backend_pid,box)
            self.root.execute('UPDATE auth.runtime_login_bindings SET operation_id=%s WHERE logical_role=%s',(UUID(uid(901)),'observer'))
        finally:locker.execute('COMMIT')
        self.joined(thread,box)
        self.assertIn('error',box,'changed owner operation published')
        error=box['error']
        if not isinstance(error,psycopg.Error) or error.sqlstate not in ('42501','55000'):raise error
        observer.execute('ROLLBACK')

# Imported base alias must not become a second discovered suite.
del PriorDraft
