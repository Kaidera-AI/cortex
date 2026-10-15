"""Explicit synthetic private owner for disposable PG regression ONLY.

No production custody, R01 signature, real HBA installation or credential
carrier is admitted. Actual P2 history and genuine finite PG logins are used.
The original frozen test bodies are imported without editing their bytes.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import UUID
from cortex_core.install import lifecycle,owner
from cortex_core.runtime import role_grants,signed_member
from cortex_core.release.contracts import thaw
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
import test_owner_proposal as proposals
import test_lifecycle_receipt as storage
from runtime_grants_fixture import Fixture,ROLE_NAMES,OPERATION,EMBEDDING_KIND,uid,MATRIX
from psycopg import sql

class Materials:
    def __init__(self,root):
        self.root=Path(root)/'materials';self.root.mkdir()
        literal=json.loads((Path(__file__).resolve().parents[1]/'runtime_grants/fixtures/pgb2-preimages.json').read_bytes())
        for field in ('hba','carrier'):(self.root/field).write_bytes(literal['originals_utf8'][field].encode())
    def snapshot(self):return {k:(self.root/k).read_bytes() for k in ('hba','carrier')}
    def restore(self,values):
        import os
        for k,raw in values.items():
            with (self.root/k).open('wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())

class NativeOwner:
    def __init__(self,test):
        self.test=test;self.helper=proposals.OwnerProposalOracle()
        self.temp=tempfile.TemporaryDirectory(dir=test.control_root,prefix='pgb2-owner-')
        test.addCleanup(self.temp.cleanup)
        self.history=lifecycle._History(Path(self.temp.name)/'history')
        self.materials=Materials(self.temp.name);self.sequence=0
        self.original=None;self.last_adapter=None
        if test._testMethodName in ('test_exact_five_originals_after_changed_live_state_and_history_reload',
                                    'test_exact_restore_after_finite_self_close'):
            test.addCleanup(self.preserve_original_witness)
    def preserve_original_witness(self):
        # Evidence export only: preserve the deciding existing P1 journal before
        # the disposable fixture directory is removed. No product store/path.
        if self.last_adapter is None:return
        import shutil
        target=self.test.control_root/('proof-'+self.test._testMethodName)
        target.mkdir()
        shutil.copytree(self.history.root,target/'history')
        live=self.last_adapter.snapshot()
        ref=lifecycle._retained_preimages(self.history,self.last_adapter.identity)
        bundle=json.loads(self.history.read(ref));original={k:self.history.read(v) for k,v in bundle['originals'].items()}
        for field in original:
            (target/(field+'-original.bin')).write_bytes(original[field])
            (target/(field+'-live.bin')).write_bytes(live[field])
        (target/'witness.json').write_text(json.dumps({'classification':'SYNTHETIC_OWNER_REAL_PG_INERT_MATERIALS',
            'test_id':self.test.id(),'preimages_ref':ref,'history_head':self.history.head()[0],
            'original_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in original.items()},
            'live_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in live.items()},
            'all_five_exact':live==original},indent=2))
    def data(self):
        self.sequence+=1;d=self.helper.data();d['originals'].update(storage.FIXTURE['originals'])
        q=json.loads(d['request_raw']);identity={**q['identity'],'operation_id':str(OPERATION),
            'installation_id':uid(1),'attempt_id':'pgb2-'+str(self.sequence)}
        q['identity']=identity;raw=proposals.encode(q)
        c=json.loads(d['context_raw']);ref=c['request_ref'];d['request_raw']=raw.decode();d['originals'][ref['path']]=raw.decode()
        self.helper.context(d,lambda c:c.update(identity=identity,nonce='pgb2-nonce-'+str(self.sequence),
            request_ref={**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}))
        self.helper.context(d,lambda c:c['effect'].update(resource_kind='database_role',effect_id='pgb2-effect-'+str(self.sequence)))
        c=json.loads(d['context_raw']);self.helper.scope(d,lambda s:s.update(identity=identity,effect=c['effect']))
        return d,identity
    def invoke(self,name,conn,**kwargs):
        t=self.test;d,identity=self.data()
        action={'prepare_runtime_roles':'prepare','bind_runtime_roles':'bind','close_finite_roles':'close','restore_runtime_roles':'restore'}[name]
        backends={r[0]:tuple(r[1:]) for r in t.root.execute('SELECT pid,usesysid,backend_start::text,datid FROM pg_stat_activity WHERE usesysid IN (SELECT oid FROM pg_roles WHERE rolname=ANY(%s)) ORDER BY pid',([ROLE_NAMES['bootstrap'],ROLE_NAMES['migrator']],)).fetchall()}
        adapter=role_grants._RoleAdapter(history=self.history,identity=identity,connection=conn if action!='restore' else None,
            recovery_connection=t.root,role_names=dict(ROLE_NAMES),installation_id=UUID(uid(1)),embedding_job_kind=EMBEDDING_KIND,
            policy=MATRIX,materials=self.materials,backends=backends)
        params={k:v for k,v in kwargs.items() if k not in ('role_names','operation_id')}
        if action=='restore':params['preimage']=thaw(params['preimage'])
        primitive=role_grants._Primitive(adapter,action,conn,params)
        # Observe scrubbed adapter exceptions without changing P2 dispositions.
        import traceback
        for method_name in ('dispatch','measure'):
            original_method=getattr(primitive,method_name)
            def observed(context,original_method=original_method):
                try:return original_method(context)
                except BaseException:
                    with (t.control_root/'adapter-errors.log').open('a') as log:traceback.print_exc(file=log)
                    raise
            setattr(primitive,method_name,observed)
        if self.original is None:self.original=adapter.snapshot()
        def inputs():
            fresh=copy.deepcopy(d);revision=self.history.current_revision()
            if revision is not None:
                self.helper.context(fresh,lambda c:c.update(state_revision=revision))
                self.helper.edit_record(fresh,'current_state_ref',lambda s:s.update(revision=revision))
            return self.helper.inputs(owner,fresh)
        double=NonAuthoritativeSignedMemberDouble(d['runtime_lock_raw'].encode(),aggregate_id=d['aggregate_id'],member_name='runtime.lock.json')
        with patch.object(lifecycle,'_installed_lifecycle',return_value=lifecycle._Bindings(self.history,{'database_role':primitive})),patch.object(owner,'_installed_owner_inputs',inputs),double.installed(signed_member):
            result=getattr(role_grants,name)(conn,**kwargs)
        self.last_adapter=adapter
        self.history=lifecycle._History(self.history.root)
        return result
    def __getattr__(self,name):
        if name not in role_grants.__all__:raise AttributeError(name)
        return lambda conn,**kwargs:self.invoke(name,conn,**kwargs)

class NativeFixture(Fixture):
    def setUp(self):
        import os,psycopg
        # Original-prefix fixture login is reset BEFORE candidate SQL. A failed
        # candidate must not turn later cases into CONNECT setup errors.
        with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as reset:
            database=reset.execute('SELECT current_database()').fetchone()[0]
            if not database.startswith('kaidera-test-grant-db-') or reset.execute('SELECT run_id FROM public.pgo_owned_fixture').fetchall()!=[(os.environ['PGO_OWNED_RUN_ID'],)]:raise RuntimeError('foreign fixture reset refused')
            # A previous BODY may have bound service CONNECT rights. Remove
            # only those owned fixture grants so the unchanged prepare body can
            # drop/recreate its originally absent probe without dependencies.
            for logical,name in ROLE_NAMES.items():
                if logical not in ('bootstrap','migrator') and reset.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():
                    reset.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {}').format(sql.Identifier(database),sql.Identifier(name)))
            reset.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO PUBLIC').format(sql.Identifier(database)))
        super().setUp()
        self.control_root=Path(os.environ['PGO_CONTROL_DIR'])
        self.phase_owner=NativeOwner(self)
    def owner_login(self):return self.login('bootstrap')
    def client(self):return self.phase_owner
    def candidate_if_present(self):
        if self._candidate:return
        self.request.close()
        with self.root.transaction():self.root.execute((Path(__file__).resolve().parents[2]/'schema/auth/007-runtime-grants.sql').read_text())
        self._candidate=True
        self.bind_current()
    def bind_current(self,*,embedding_kind=EMBEDDING_KIND,operation=OPERATION):
        with self.owner_login() as conn:
            receipt=self.phase_owner.bind_runtime_roles(conn,installation_id=UUID(uid(1)),role_names=dict(ROLE_NAMES),
                operation_id=operation,embedding_job_kind=embedding_kind,activate=True)
        if not self.request.closed:self.request.close()
        self.request=self.login('api_request',request_role=True)
        return receipt
