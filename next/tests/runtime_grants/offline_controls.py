"""Run exact frozen PG-O bodies on static catalog/port substitutions.

This is assertion adequacy only, not PostgreSQL/product mutation qualification.
No DB fixture setUp, connection, SQL execution, host or product process runs.
Hale's catalog-row method and shared test_receipts are reused.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
NEXT=HERE.parents[1]
sys.path[:0]=[str(HERE),str(NEXT/'src'),str(NEXT/'tests')]
import psycopg
from cortex_core.auth import AuthError
from cortex_core.migrations import MigrationError
from support import MATRIX,ROLE_NAMES,APP_SCHEMAS,TABLE_PRIVS,REQUEST,OPERATION,Fixture,expected,uid
import test_grants as grants
import test_ports as ports
import test_lifecycle as lifecycle
import test_repair as repair
import test_receipts as receipts

LOGICAL={value:key for key,value in ROLE_NAMES.items()}
TABLES={row['name']:row for row in MATRIX['tables']}
FUNCTIONS={row['signature']:row for row in MATRIX['functions']}


class Rows:
    def __init__(self,rows=(),rowcount=0):self.rows=list(rows);self.rowcount=rowcount
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows
    def __iter__(self):return iter(self.rows)


class StaticRows:
    def __init__(self,control,bad):
        self.control,self.bad=control,bad
        self.queries=[];self.substitutions=[];self.creator=None
        self.fence=control.get('fence',7);self.released=False;self.revision=1
        self.closed=False;self.drift=False;self.bind_calls=0
        self.ledger=[('core-0001','1'*64)]
    @property
    def mode(self):return self.control['mode'] if self.bad else 'correct'
    def changed(self,name,before,after):
        self.substitutions.append({'boundary':name,'before':before,'after':after})
        return after
    @contextmanager
    def transaction(self):
        previous=self.creator
        try:yield
        finally:self.creator=previous
    def execute(self,query,args=(),logical=None):
        q=' '.join((query.as_string() if hasattr(query,'as_string') else str(query)).split())
        self.queries.append({'query':q,'args':list(map(str,args)),'session':logical})
        if q.startswith('SELECT rolcanlogin,'):
            name=LOGICAL[args[0]];p=MATRIX['roles'][name]
            row=tuple(p[k] for k in ('login','superuser','bypassrls','inherit','createdb','createrole','replication'))
            if self.mode=='service_superuser' and name=='api_request':row=self.changed('role flags',row,row[:1]+(True,)+row[2:])
            return Rows([row])
        if q.startswith('SELECT granted.rolname,'):
            row=[(REQUEST,True,False,False)] if args[0]==ROLE_NAMES['api_request'] else []
            if self.mode=='membership_admin' and row:row=self.changed('membership admin option',row,[(REQUEST,True,False,True)])
            return Rows(row)
        if q.startswith('SELECT count(*) FROM pg_auth_members'):return Rows([(0,)])
        if q.startswith('SELECT p.prosecdef,'):
            fn=FUNCTIONS[args[0]];vol='v';path='pg_catalog, auth, core, coordination, pg_temp'
            if self.mode=='stable_health' and args[0]=='coordination.runtime_lease_health(uuid)':vol=self.changed('health volatility','v','s')
            if self.mode=='untrusted_path':path=self.changed('search_path',path,'pg_catalog, evil_untrusted, pg_temp')
            return Rows([(True,ROLE_NAMES['verifier_owner'],fn['return_type'],vol,['search_path='+path])])
        if q.startswith('SELECT pg_get_userbyid(nspowner)'):return Rows([(ROLE_NAMES['migrator'],)])
        if q.startswith('SELECT has_function_privilege'):
            name=LOGICAL[args[0]];signature=args[1] if len(args)>1 else 'auth.pgo_default_canary()'
            priv=args[2] if len(args)>2 else 'EXECUTE'
            if signature=='auth.pgo_default_canary()':
                value=False
                if self.mode=='future_function' and name==self.control.get('role','api_request') and self.creator==ROLE_NAMES[self.control.get('creator','migrator')]:
                    value=self.changed('future function ACL',False,True)
            else:
                value=expected(FUNCTIONS[signature]['direct_privileges'][name],'GRANT_OPTION' if 'GRANT OPTION' in priv else 'EXECUTE')
                if self.mode=='public_execute' and name=='probe' and signature=='coordination.refuse_fence_regression()':value=self.changed('existing function PUBLIC execute',value,True)
                if self.mode=='private_wrong_recipient' and name=='probe' and signature=='coordination.runtime_observe(uuid)':value=self.changed('private function recipient',value,True)
            return Rows([(value,)])
        if q.startswith('SELECT has_type_privilege'):
            name=LOGICAL[args[0]];obj=args[1];priv=args[2] if len(args)>2 else ('USAGE WITH GRANT OPTION' if 'GRANT OPTION' in q else 'USAGE')
            if obj=='auth.pgo_default_type':
                value=False
                if self.mode=='future_type' and name==self.control.get('role','api_request') and self.creator==ROLE_NAMES[self.control.get('creator','migrator')]:value=self.changed('future type ACL',False,True)
            else:value=(MATRIX['roles'][name]['superuser'] or name==MATRIX['type']['owner']) if 'GRANT OPTION' in priv else expected(MATRIX['type']['direct_privileges'][name],'USAGE')
            return Rows([(value,)])
        if q.startswith('SELECT has_schema_privilege'):
            name=LOGICAL[args[0]];schema,priv=args[1:];finite=MATRIX['roles'][name]['superuser']
            value=finite if 'GRANT OPTION' in priv else finite or any(x.split()[:2]==[priv,schema] for x in MATRIX['schemas'].get(name,[]))
            if self.mode=='service_create' and name=='observer' and schema=='core' and priv=='CREATE':value=self.changed('schema CREATE',value,True)
            return Rows([(value,)])
        if q.startswith('SELECT has_database_privilege'):
            name=LOGICAL[args[0]];priv=args[1];finite=MATRIX['roles'][name]['superuser']
            value=finite if 'GRANT OPTION' in priv else expected(MATRIX['database']['direct_privileges'][name],priv)
            if self.mode=='request_connect' and name=='request_group' and priv=='CONNECT':value=self.changed('request CONNECT',value,True)
            if self.mode=='service_temp' and name=='observer' and priv=='TEMP':value=self.changed('service TEMP',value,True)
            return Rows([(value,)])
        if q.startswith('SELECT has_table_privilege'):
            name=LOGICAL[args[0]]
            obj,priv=(args[1],args[2]) if len(args)==3 else ('auth.pgo_default_table' if 'auth.pgo_default_table' in q else 'core.schema_migrations',args[1])
            if obj=='auth.pgo_default_table':value=False
            elif obj=='core.schema_migrations':value=MATRIX['roles'][name]['superuser']
            elif obj==MATRIX['view']['name']:
                value=(MATRIX['roles'][name]['superuser'] or name==MATRIX['view']['owner']) if 'GRANT OPTION' in priv else expected(MATRIX['view']['direct_privileges'][name],priv)
            else:value=expected(TABLES[obj]['direct_privileges'][name],'GRANT_OPTION' if 'GRANT OPTION' in priv else priv)
            if self.mode=='supervisor_dml' and name=='supervisor' and obj=='coordination.supervisor_leases' and priv=='UPDATE':value=self.changed('supervisor raw DML',value,True)
            if self.mode=='request_view' and name=='request_group' and obj==MATRIX['view']['name'] and priv=='SELECT':value=self.changed('request view SELECT',value,True)
            return Rows([(value,)])
        if q.startswith('SELECT has_sequence_privilege'):
            name=LOGICAL[args[0]];priv=args[2]
            value=(MATRIX['roles'][name]['superuser'] or name==MATRIX['sequence']['owner']) if 'GRANT OPTION' in priv else expected(MATRIX['sequence']['direct_privileges'][name],priv)
            if self.mode=='sequence_update' and name=='request_group' and priv=='UPDATE':value=self.changed('sequence UPDATE',value,True)
            return Rows([(value,)])
        if q.startswith('SELECT relrowsecurity,'):return Rows([(True,True)])
        if q.startswith('SELECT polpermissive'):
            return Rows([(self.changed('restrictive policy',False,True) if self.mode=='permissive_policy' else False,)])
        if q.startswith('SET LOCAL ROLE '):self.creator=q.removeprefix('SET LOCAL ROLE ').strip('"');return Rows()
        if q.startswith('SELECT role_oid,'):
            if 'ORDER BY' in q:return Rows([(101,ROLE_NAMES['api_request'],uid(1),True,self.revision,str(OPERATION))])
            return Rows([(101,ROLE_NAMES['observer'],uid(1),True,self.revision)])
        if q.startswith('UPDATE auth.runtime_login_bindings SET revision=revision+1'):
            self.revision+=1;return Rows(rowcount=1)
        if q.startswith('SELECT oid,rolname,rolcanlogin,'):
            return Rows([(101,ROLE_NAMES['request_group'],self.drift,False,False,False,False)])
        if q.startswith('ALTER ROLE '):self.drift=True;return Rows()
        if q.startswith('SELECT rolname,rolcanlogin,'):
            return Rows([(name,False,False,False,False,False) for name in sorted((ROLE_NAMES['bootstrap'],ROLE_NAMES['migrator']))])
        if q.startswith('SELECT count(*) FROM pg_stat_activity'):
            value=1 if self.mode=='undrained_session' else 0
            if value:self.changed('recorded backend count',0,1)
            return Rows([(value,)])
        if q.startswith('SELECT migration_id,sha256'):return Rows(self.ledger)
        if q.startswith('INSERT INTO core.schema_migrations'):self.ledger.append(('pgo-unknown-applied','0'*64));return Rows()
        if q.startswith('UPDATE core.schema_migrations SET sha256='):self.ledger[0]=('core-0001','0'*64);return Rows()
        if q.startswith("SELECT n.nspname||'.'||c.relname"):return Rows([('core.records',)])
        if q.startswith('SELECT row_to_json'):return Rows([('{"id":"synthetic-valid-row"}',)])
        if q.startswith('SELECT expires_at>'):return Rows([(True,)])
        if q.startswith('SELECT * FROM coordination.supervisor_leases'):
            return Rows([(uid(1),uid(100),self.fence,'synthetic-future-expiry')])
        if q.startswith('SELECT fence,expires_at<='):
            if self.mode=='deleted_lease':return Rows([self.changed('retained lease row',(self.fence,True),None)])
            return Rows([(self.fence,True)])
        if q.startswith('SELECT fence FROM coordination.supervisor_leases'):return Rows([(self.fence,)])
        if q.startswith('SELECT t.tgfoid::'):
            row=('coordination.refuse_fence_regression()','O')
            return Rows([self.changed('trigger attachment',row,None) if self.mode=='missing_trigger' else row])
        if q.startswith('UPDATE coordination.supervisor_leases SET fence='):
            if self.mode=='missing_trigger':self.fence=self.changed('regression allowed',self.fence,args[0]);return Rows()
            raise psycopg.errors.ObjectNotInPrerequisiteState('synthetic regression refusal')
        if q.startswith('SELECT coordination.runtime_lease_acquire'):
            self.fence+=1;self.released=False;return Rows([(self.fence,)])
        if q.startswith('SELECT coordination.runtime_lease_release'):
            self.released=True;return Rows([(True,)])
        if q.startswith('SELECT coordination.runtime_lease_assert_live'):
            if self.mode=='definer_identity':
                self.changed('own session admission',True,'42501')
                raise psycopg.errors.InsufficientPrivilege('synthetic wrong current_user comparison')
            return Rows([(True,)])
        if q.startswith('SELECT coordination.runtime_lease_health'):
            value=self.fence
            if self.mode=='fence_float':value=self.changed('fence type',value,float(value))
            if self.mode=='fence_bool':value=self.changed('fence type',value,True)
            return Rows([({'state':'expired' if self.released else 'live','fence':value},)])
        if q.startswith('SELECT coordination.runtime_observe'):
            if args and args[0]==uid(20):raise psycopg.errors.InsufficientPrivilege('synthetic foreign installation')
            if self.control['mode']=='fault_zero':
                if self.bad:self.changed('observer fault',{'sqlstate':'55000'},'successful zero')
                else:raise psycopg.errors.ObjectNotInPrerequisiteState('synthetic valid-bound aggregate fault')
            pending,embedding=3,1
            if self.mode=='wrong_pending':pending=self.changed('pending-only count',3,11)
            if self.mode=='count_float':pending=self.changed('count type',3,3.0)
            if self.mode=='count_bool':embedding=self.changed('count type',1,True)
            return Rows([({'database_bytes':1,'pending_jobs':pending,'pending_embedding_jobs':embedding,'lease':{'state':'absent','fence':None}},)])
        if q.startswith('SELECT auth.first_identity'):
            if self.mode=='installer_admission':self.changed('foreign installer admission','42501',True);return Rows([(True,)])
            raise psycopg.errors.InsufficientPrivilege('synthetic foreign installer')
        if q.startswith('SELECT count(*) FROM ') and logical=='observer':raise psycopg.errors.InsufficientPrivilege('synthetic raw table denial')
        if q.startswith(('CREATE ','DROP ','GRANT ','REVOKE ','RESET ROLE','BEGIN','COMMIT','ROLLBACK','SET statement_timeout','LOCK TABLE ','INSERT INTO coordination.jobs','ALTER TABLE coordination.jobs','SELECT pg_sleep')):return Rows()
        raise RuntimeError('unexpected static query: '+repr((q,args,logical)))


class Connection:
    def __init__(self,root,logical):self.root,self.logical=root,logical;self.info=NS(backend_pid={'bootstrap':10,'migrator':11,'observer':12,'supervisor':13}.get(logical,14))
    def execute(self,query,args=()):return self.root.execute(query,args,self.logical)
    def transaction(self):return self.root.transaction()
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*args):pass


class Client:
    def __init__(self,root):self.root=root
    def bind_runtime_roles(self,*args,**kwargs):
        if str(kwargs.get('installation_id'))==uid(20):raise ValueError('foreign synthetic operation')
        return {}
    def close_finite_roles(self,*args,**kwargs):self.root.closed=True
    def restore_runtime_roles(self,*args,**kwargs):
        if self.root.mode=='restore_shortcut':self.root.changed('cluster role restoration',False,True)
        else:self.root.drift=False


class FrozenCase(unittest.TestCase):
    # These are fixture boundaries only. All deciding assertion methods and
    # helpers below are the actual imported frozen source, not copies.
    candidate_if_present=lambda self:None
    require=lambda self,*args:None
    require_candidate=lambda self:None
    private=lambda self,*args:None
    note=lambda self,*args:None
    has=lambda self,*args:False
    allowed=Fixture.allowed
    refusal=Fixture.refusal
    seed_jobs=ports.Ports.seed_jobs
    _empty_identity=lambda self,*args:None
    _closed_box=repair.Repair._closed_box
    _revision_barrier=repair.Repair._revision_barrier
    _queued_expiry=repair.Repair._queued_expiry
    _observer_fault=repair.Repair._observer_fault
    _ledger_fault=repair.Repair._ledger_fault
    def client(self):return Client(self.root)
    def prepare(self):return self.client(),{'preimage':{}}
    def login(self,logical,**kwargs):
        if self.root.closed:raise psycopg.OperationalError('synthetic NOLOGIN')
        return Connection(self.root,logical)
    def bind_current(self,**kwargs):
        if kwargs.get('operation',OPERATION)!=OPERATION:raise ValueError('synthetic other operation')
        self.root.bind_calls+=1
        if self.root.mode=='reentry_revision' and self.root.bind_calls>1:
            self.root.revision=self.root.changed('reentry revision',self.root.revision,self.root.revision+1)
        return {}
    @contextmanager
    def auth(self,*args,**kwargs):
        if self.root.mode=='foreign_session':
            self.root.changed('foreign valid credential admission','forbidden','admitted');yield NS()
        else:raise AuthError('forbidden')
    def lease(self,conn=None,**kwargs):return conn or self.login('supervisor'),self.root.fence
    def background(self,*args,**kwargs):
        if self.root.control['mode']=='stale_revision':
            box={'value':{'state':'absent','fence':None}} if self.root.bad else {'error':psycopg.errors.ObjectNotInPrerequisiteState('synthetic stale revision')}
            if self.root.bad:self.root.changed('captured revision refusal','55000','stale success')
        else:
            value=self.root.changed('post-lock expiry',False,True) if self.root.mode=='queued_expiry' else False
            box={'value':value}
        return self.login(args[0]),99,box,NS(join=lambda *a:None,is_alive=lambda:False)
    wait_blocked=lambda self,*args:None
    joined=lambda self,thread,box:box


def controls():
    health=next(x for x in MATRIX['functions'] if x['signature']=='coordination.runtime_lease_health(uuid)')
    values=[
      ('M01','service_superuser','test_role__api_request',grants.role_case('api_request')),
      ('M02','membership_admin','test_role__api_request',grants.role_case('api_request')),
      ('M03','foreign_session','test_cross_installation_valid_credential_is_not_session_authority',grants.Grants.test_cross_installation_valid_credential_is_not_session_authority),
      ('M04','definer_identity','test_supervisor_own_session_admits_positive_lease',repair.Repair.test_supervisor_own_session_admits_positive_lease),
      ('M05','stale_revision','test_observer_final_revision_only_barrier',repair.Repair.test_observer_final_revision_only_barrier),
      ('M06','supervisor_dml','test_tables__supervisor',grants.table_case('supervisor')),
      ('M07','public_execute','test_functions__probe',grants.function_case('probe')),
      ('M08','permissive_policy','test_restrictive_request_policy_remains_restrictive',repair.Repair.test_restrictive_request_policy_remains_restrictive),
      ('M09','request_view','test_other_objects__request_group',grants.other_case('request_group')),
      ('M10','sequence_update','test_other_objects__request_group',grants.other_case('request_group')),
      ('M11','queued_expiry','test_queued_expiry__release',getattr(repair.Repair,'test_queued_expiry__release')),
      ('M12','deleted_lease','test_release_retains_row_and_next_acquire_increases_fence',ports.Ports.test_release_retains_row_and_next_acquire_increases_fence),
      ('M13','wrong_pending','test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign',ports.Ports.test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign),
      ('M14','fault_zero','test_valid_bound_observer_aggregate_error_never_success_zero',repair.Repair.test_valid_bound_observer_aggregate_error_never_success_zero),
      ('M15','installer_admission','test_installer_other_installation_is_not_admitted_by_name_only',grants.Grants.test_installer_other_installation_is_not_admitted_by_name_only),
      ('M16','service_create','test_other_objects__observer',grants.other_case('observer')),
      ('M17','undrained_session','test_close_disables_both_finite_roles_and_drains_real_sessions',lifecycle.Lifecycle.test_close_disables_both_finite_roles_and_drains_real_sessions),
      ('M18','reentry_revision','test_bind_same_operation_preserves_oid_revision_and_other_operation_refuses',lifecycle.Lifecycle.test_bind_same_operation_preserves_oid_revision_and_other_operation_refuses),
      ('M19','bad_ledger','test_unknown_applied_ledger_row_refuses_unchanged',repair.Repair.test_unknown_applied_ledger_row_refuses_unchanged),
      ('M20','restore_shortcut','test_restore_reconciles_cluster_role_preimage_separately_from_database',lifecycle.Lifecycle.test_restore_reconciles_cluster_role_preimage_separately_from_database),
      ('M21','missing_trigger','test_generic_fence_trigger_is_attached_and_rejects_positive_regression',ports.Ports.test_generic_fence_trigger_is_attached_and_rejects_positive_regression),
      ('F02-health','stable_health','test_signature__coordination__runtime_lease_health',grants.signature_case(health)),
      ('F02-path','untrusted_path','test_signature__coordination__runtime_lease_health',grants.signature_case(health)),
      ('F06-connect','request_connect','test_other_objects__request_group',grants.other_case('request_group')),
      ('F06-execute','private_wrong_recipient','test_functions__probe',grants.function_case('probe')),
      ('F06-temp','service_temp','test_other_objects__observer',grants.other_case('observer')),
      ('F03-health','stale_revision','test_health_final_revision_only_barrier',repair.Repair.test_health_final_revision_only_barrier),
      ('F04-assert-expiry','queued_expiry','test_queued_expiry__assert_live',getattr(repair.Repair,'test_queued_expiry__assert_live')),
      ('F05-timeout','fault_zero','test_valid_bound_observer_timeout_never_success_zero',repair.Repair.test_valid_bound_observer_timeout_never_success_zero),
      ('F07-checksum','bad_ledger','test_changed_applied_checksum_refuses_unchanged',repair.Repair.test_changed_applied_checksum_refuses_unchanged),
      ('F10-count-bool','count_bool','test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign',ports.Ports.test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign),
      ('F10-count-float','count_float','test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign',ports.Ports.test_observer_pending_embedding_jobs_is_not_total_jobs_or_foreign),
      ('F10-fence-bool','fence_bool','test_release_retains_row_and_next_acquire_increases_fence',ports.Ports.test_release_retains_row_and_next_acquire_increases_fence),
      ('F10-fence-float','fence_float','test_release_retains_row_and_next_acquire_increases_fence',ports.Ports.test_release_retains_row_and_next_acquire_increases_fence),
    ]
    result=[dict(id=i,mode=m,name=n,method=f) for i,m,n,f in values]
    for kind in ('function','type'):
        for creator in ('migrator','verifier_owner'):
            for role in ROLE_NAMES:
                if MATRIX['roles'][role]['superuser'] or role==creator:continue
                result.append(dict(id='F01-'+kind+'-'+creator+'-'+role,mode='future_'+kind,
                    name='test_public_function_and_type_defaults_closed_for_both_creators',
                    method=grants.Grants.test_public_function_and_type_defaults_closed_for_both_creators,
                    creator=creator,role=role))
    for value in result:
        if value['mode']=='fence_bool':value['fence']=1
        if value['id']=='M21':value['fence']=2
        method=value['method'];module=method.__module__
        cls={'test_grants':'Grants','test_ports':'Ports','test_lifecycle':'Lifecycle','test_repair':'Repair'}[module]
        value['node']=module+'.'+cls+'.'+value['name'];value['class_name']=cls;value['module']=module
    return result


def main():
    output=Path(sys.argv[1]).resolve();output.mkdir(parents=True,exist_ok=True)
    denied=[]
    def audit(event,args):
        if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.fork','os.posix_spawn'):
            denied.append(event);raise RuntimeError('offline controls forbid '+event)
    sys.addaudithook(audit)
    files=sorted(HERE.glob('*.py'))+[NEXT/'tests/test_receipts.py']
    before={str(p.relative_to(NEXT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    rows=[];errors=[]
    for control in controls():
        cls=type(control['class_name'],(FrozenCase,),{'__module__':control['module'],control['name']:control['method']})
        stages=[]
        for label,bad in (('baseline',False),('wrong_row',True),('restore',False)):
            case=cls(control['name']);case.root=StaticRows(control,bad)
            def applied(*args,**kwargs):
                if bad:
                    case.root.changed('applied ledger refusal','MigrationError','accepted');return []
                raise MigrationError('synthetic unknown applied ledger')
            with patch.object(repair,'apply_migrations',applied),patch.object(psycopg,'connect',side_effect=RuntimeError('real PG connection forbidden')):
                result=unittest.TextTestRunner(verbosity=2,resultclass=receipts.AssertionResult).run(unittest.TestSuite([case]))
            record={'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':t.id(),'traceback':tb} for t,tb in result.errors]}
            run=NS(returncode=0 if result.wasSuccessful() else 1,stdout=receipts.MARKER+json.dumps(record))
            verdict=receipts.classify(run,{control['node']})
            if denied:verdict='inconclusive'
            data=dict(record,control=control['id'],label=label,classification=verdict,queries=case.root.queries,
                substitutions=case.root.substitutions,source_hashes=before,denied_effects=list(denied),skips=result.skipped,
                final_state={key:getattr(case.root,key) for key in ('fence','released','revision','closed','drift','bind_calls','ledger','creator')})
            (output/(control['id']+'-'+label+'.json')).write_text(json.dumps(data,indent=2)+'\n');stages.append(data)
        exact_restore=(stages[0]['queries']==stages[2]['queries']
                       and stages[0]['final_state']==stages[2]['final_state']
                       and not stages[0]['substitutions'] and not stages[2]['substitutions'])
        valid=stages[0]['classification']=='survived' and stages[1]['classification']=='killed' and stages[2]['classification']=='survived' and bool(stages[1]['substitutions']) and exact_restore
        if not valid:errors.append({'id':control['id'],'classifications':[x['classification'] for x in stages],'errors':[x['errors'] for x in stages]})
        rows.append({'id':control['id'],'node':control['node'],'baseline':stages[0]['classification'],'wrong_row':stages[1]['classification'],'restore':stages[2]['classification'],'substitutions':len(stages[1]['substitutions']),'exact_row_state_and_trace_restore':exact_restore,'valid':valid})
    after={str(p.relative_to(NEXT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    assert before==after,'oracle source drift during controls'
    value={'scope':'static catalog/port-row assertion mutants only; no PostgreSQL/product implementation qualification',
           'families':21,'families_killed':sum(any(r['id']==f'M{i:02}' and r['valid'] for r in rows) for i in range(1,22)),
           'controls':len(rows),'valid_controls':sum(r['valid'] for r in rows),'errors':errors,'denied_effects':denied,
           'source_hashes':before,'source_restoration_exact':True,'rows':rows}
    (output/'SUMMARY.json').write_text(json.dumps(value,indent=2)+'\n');print(json.dumps(value,indent=2))
    return 0 if not errors and not denied else 1


if __name__=='__main__':raise SystemExit(main())
