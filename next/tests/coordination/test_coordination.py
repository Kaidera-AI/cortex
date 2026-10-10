"""C05 private durable jobs on real PG, frozen before the port exists."""
import hashlib
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import UUID
import psycopg
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture,READ_A,WRITE_A,OWNER_A,READ_B,API,REQUEST,uid
from cortex_core.auth import AuthError
try:
    from cortex_core import coordination as port
except ImportError:
    port=None

class JobAdapters(Fixture):
    def adapter(self,key=WRITE_A,installation=1,connection=None):
        self.assertIsNotNone(port,'C05 coordination adapter is missing')
        return port.Jobs(connection or self.request,key,UUID(uid(installation)),UUID(uid(3)))
    def create(self,job=60,kind='extract',key='create',recipient=None):
        return self.adapter(OWNER_A).create(UUID(uid(job)),kind,b'exact intent\x00\xff',key,recipient_principal=recipient)
    def claim(self,job=60,key='claim',ttl=60):return self.adapter().claim(UUID(uid(job)),key,ttl_seconds=ttl)
    def error(self,code,call):
        self.assertIsNotNone(port,'C05 coordination adapter is missing')
        with self.assertRaises(port.JobError) as caught:call()
        self.assertEqual(caught.exception.code,code)
    def expire_lease(self,job=60):
        self.admin.execute("UPDATE coordination.leases SET expires_at=clock_timestamp()-interval '1 second' WHERE resource_id=%s",(uid(job),))
    def other(self):
        c=psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',API+'@'),autocommit=True)
        c.execute(f'SET ROLE "{REQUEST}"');return c
    def test_create_get_preserves_exact_intent_bytes(self):
        self.create();v=self.adapter(READ_A).get(UUID(uid(60)))
        self.assertEqual((v.job_id,v.kind,v.state,v.body),(UUID(uid(60)),'extract','pending',b'exact intent\x00\xff'))
    def test_create_replays_after_commit_and_changed_request_refuses(self):
        a=self.create();self.assertEqual(a,self.create())
        self.error('conflict',lambda:self.adapter(OWNER_A).create(UUID(uid(60)),'extract',b'changed','create'))
        self.error('conflict',lambda:self.create(job=61))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.jobs').fetchone()[0],1)
    def test_owner_controls_and_read_cannot_write(self):
        a=self.adapter()
        with self.assertRaises(AuthError):a.create(UUID(uid(60)),'extract',b'bad','bad')
        self.create()
        with self.assertRaises(AuthError):self.adapter(READ_A).claim(UUID(uid(60)),'read')
        with self.assertRaises(AuthError):a.cancel(UUID(uid(60)),'cancel')
        with self.assertRaises(AuthError):a.retry(UUID(uid(60)),'retry')
    def test_claim_binds_verified_principal_and_fence(self):
        self.create();a=self.claim()
        self.assertEqual((a.holder,a.attempt_number,a.fence),(UUID(uid(10)),1,1))
        row=self.admin.execute('SELECT worker_id,fence FROM coordination.job_attempts WHERE id=%s',(a.attempt_id,)).fetchone()
        self.assertEqual(row,(uid(10),1));self.assertEqual(self.adapter().get(UUID(uid(60))).state,'running')
    def test_explicit_recipient_refuses_other_principal(self):
        self.create(recipient=UUID(uid(12)));self.error('forbidden',lambda:self.claim())
        a=self.adapter(OWNER_A).claim(UUID(uid(60)),'owner-claim');self.assertEqual(a.holder,UUID(uid(12)))
    def test_concurrent_claim_has_one_winner(self):
        self.create()
        def call(i):
            with self.other() as c:
                try:return self.adapter(connection=c).claim(UUID(uid(60)),'claim-'+str(i)).attempt_number
                except port.JobError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(call,range(2)))
        self.assertEqual(sorted(r,key=str),[1,'conflict'])
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_attempts').fetchone()[0],1)
    def test_same_key_claim_replay_is_one_attempt(self):
        self.create();a=self.claim();self.assertEqual(a,self.claim())
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_attempts').fetchone()[0],1)
    def test_complete_preserves_exact_result_and_is_immutable(self):
        self.create();a=self.claim();body=b'exact result\x00\xff'
        r=self.adapter().complete(a.job_id,a.attempt_id,a.fence,body,'done')
        self.assertEqual(r.state,'succeeded');self.assertEqual(r,self.adapter().complete(a.job_id,a.attempt_id,a.fence,body,'done'))
        row=self.admin.execute('SELECT p.body,p.sha256 FROM coordination.job_results r JOIN core.payloads p ON (p.tenant_id,p.project_id,p.id)=(r.tenant_id,r.project_id,r.payload_ref)').fetchone()
        self.assertEqual(row,(body,hashlib.sha256(body).hexdigest()))
        self.error('conflict',lambda:self.adapter().complete(a.job_id,a.attempt_id,a.fence,b'changed','done'))
        with self.assertRaises(psycopg.Error):self.admin.execute('UPDATE coordination.job_results SET outcome=outcome')
    def test_nonholder_and_wrong_attempt_or_fence_cannot_complete(self):
        self.create();a=self.claim()
        self.error('forbidden',lambda:self.adapter(OWNER_A).complete(a.job_id,a.attempt_id,a.fence,b'bad','other'))
        for attempt,fence in ((UUID(uid(99)),a.fence),(a.attempt_id,a.fence+1)):
            self.error('conflict',lambda attempt=attempt,fence=fence:self.adapter().complete(a.job_id,attempt,fence,b'bad','wrong'))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
    def test_expiry_refuses_completion_and_needs_explicit_retry(self):
        self.create();a=self.claim();self.expire_lease()
        self.error('conflict',lambda:self.adapter().complete(a.job_id,a.attempt_id,a.fence,b'late','late'))
        self.assertEqual(self.adapter(OWNER_A).expire(a.job_id,'expire').state,'unresolved')
        self.error('conflict',lambda:self.claim(key='automatic'))
        self.adapter(OWNER_A).retry(a.job_id,'retry');b=self.claim(key='new-claim')
        self.assertEqual((b.attempt_number,b.fence),(2,2))
        self.error('conflict',lambda:self.adapter().complete(a.job_id,a.attempt_id,a.fence,b'stale','stale'))
        self.assertEqual(self.admin.execute('SELECT outcome FROM coordination.job_results WHERE attempt_id=%s',(a.attempt_id,)).fetchone()[0],'unresolved')
    def test_cancel_never_becomes_success_and_is_terminal(self):
        self.create();a=self.claim();self.adapter(OWNER_A).cancel(a.job_id,'cancel')
        self.error('conflict',lambda:self.adapter().complete(a.job_id,a.attempt_id,a.fence,b'bad','bad'))
        self.error('conflict',lambda:self.adapter(OWNER_A).retry(a.job_id,'retry'))
        v=self.adapter().get(a.job_id);self.assertEqual((v.state,v.cancel_requested),('canceled',True))
    def test_cancel_complete_race_has_one_terminal_state(self):
        self.create();a=self.claim()
        def call(i):
            with self.other() as c:
                try:
                    if i:return self.adapter(OWNER_A,connection=c).cancel(a.job_id,'cancel').state
                    return self.adapter(connection=c).complete(a.job_id,a.attempt_id,a.fence,b'done','done').state
                except port.JobError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(call,range(2)))
        self.assertEqual(r.count('conflict'),1)
        state=self.adapter().get(a.job_id).state;self.assertIn(state,('canceled','succeeded'));self.assertIn(state,r)
        self.assertEqual(self.admin.execute('SELECT outcome FROM coordination.job_results WHERE attempt_id=%s',(a.attempt_id,)).fetchone()[0],state)
    def test_failure_after_actual_result_insert_rolls_back(self):
        self.create();a=self.claim();before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        real=port._append_result
        def fail(*args,**kwargs):real(*args,**kwargs);raise RuntimeError('synthetic after-result failure')
        with patch.object(port,'_append_result',side_effect=fail),self.assertRaises(RuntimeError):self.adapter().complete(a.job_id,a.attempt_id,a.fence,b'partial','partial')
        self.assertEqual(self.adapter().get(a.job_id).state,'running')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],before)
    def test_cross_tenant_job_is_hidden_and_cannot_claim(self):
        self.create();a=self.adapter(READ_B,20);self.assertIsNone(a.get(UUID(uid(60))))
        self.error('not_found',lambda:self.adapter(OWNER_A).claim(UUID(uid(99)),'missing'))
    def test_revoked_credential_cannot_replay_claim(self):
        self.create();self.claim()
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(WRITE_A).hexdigest(),))
        with self.assertRaises(AuthError):self.claim()
    def test_invalid_inputs_and_ttl_refuse_without_partial_rows(self):
        a=self.adapter(OWNER_A)
        for call in (lambda:a.create('bad','extract',b'x','k'),lambda:a.create(UUID(uid(60)),'extract\n',b'x','k'),lambda:a.create(UUID(uid(60)),'extract','text','k')):
            self.error('invalid_input',call)
        self.create()
        for ttl in (0,3601,True):self.error('invalid_input',lambda ttl=ttl:self.claim(ttl=ttl))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_attempts').fetchone()[0],0)
    def test_release_is_unresolved_abandon_and_fail_are_distinct(self):
        self.create();a=self.claim();r=self.adapter().release(a.job_id,a.attempt_id,a.fence,'release')
        self.assertEqual((r.state,r.reason),('unresolved','released_unproven'))
        self.adapter(OWNER_A).retry(a.job_id,'retry');b=self.claim(key='again')
        r=self.adapter().abandon(b.job_id,b.attempt_id,b.fence,'abandon');self.assertEqual((r.state,r.reason),('canceled','abandoned'))
        self.create(job=61,key='create2');c=self.claim(job=61,key='claim2');r=self.adapter().fail(c.job_id,c.attempt_id,c.fence,b'failure','failed')
        self.assertEqual((r.state,r.reason),('failed','failed'))
    def test_stable_bounded_pagination(self):
        for n in (62,60,61):self.create(job=n,key='create'+str(n))
        page=self.adapter(READ_A).list(limit=2);self.assertEqual([r.job_id for r in page],[UUID(uid(60)),UUID(uid(61))])
        self.assertEqual([r.job_id for r in self.adapter(READ_A).list(limit=2,after=page[-1].job_id)],[UUID(uid(62))])
        self.error('invalid_input',lambda:self.adapter().list(limit=0));self.error('invalid_input',lambda:self.adapter().list(limit=101))
    def test_handoff_return_requires_independent_accept(self):
        self.create(kind='handoff');a=self.claim();body=b'actual handback\x00\xff'
        self.assertEqual(self.adapter().return_result(a.job_id,a.attempt_id,a.fence,body,'return').state,'running')
        self.error('conflict',lambda:self.adapter().complete(a.job_id,a.attempt_id,a.fence,body,'bypass'))
        with self.assertRaises(AuthError):self.adapter().accept(a.job_id,a.attempt_id,a.fence,'accept')
        self.assertEqual(self.adapter(OWNER_A).accept(a.job_id,a.attempt_id,a.fence,'accept').state,'succeeded')
        self.assertEqual(self.admin.execute('SELECT p.body FROM coordination.job_results r JOIN core.payloads p ON p.id=r.payload_ref AND p.tenant_id=r.tenant_id AND p.project_id=r.project_id').fetchone()[0],body)
    def test_owner_worker_cannot_self_accept_or_rework(self):
        self.create(kind='handoff',recipient=UUID(uid(12)));a=self.adapter(OWNER_A).claim(UUID(uid(60)),'claim')
        self.adapter(OWNER_A).return_result(a.job_id,a.attempt_id,a.fence,b'return','return')
        for method in ('accept','rework'):
            self.error('forbidden',lambda method=method:getattr(self.adapter(OWNER_A),method)(a.job_id,a.attempt_id,a.fence,method))
        self.assertEqual(self.adapter().get(a.job_id).state,'running')
    def test_rework_needs_explicit_retry_and_new_attempt(self):
        self.create(kind='handoff');a=self.claim();self.adapter().return_result(a.job_id,a.attempt_id,a.fence,b'rework','return')
        self.assertEqual(self.adapter(OWNER_A).rework(a.job_id,a.attempt_id,a.fence,'rework').state,'failed')
        self.error('conflict',lambda:self.claim(key='auto'))
        self.adapter(OWNER_A).retry(a.job_id,'retry');b=self.claim(key='again');self.assertEqual(b.fence,2)
        self.error('conflict',lambda:self.adapter(OWNER_A).accept(a.job_id,a.attempt_id,a.fence,'stale'))
    def test_claim_budget_alias_is_explicitly_not_enforced(self):
        self.create();r=self.adapter().claim_with_budget(UUID(uid(60)),'claim',ttl_seconds=60,budget=0)
        self.assertEqual((r.claim.fence,r.budget_status),(1,'not_enforced'))
    def test_closed_core_connection_has_typed_failure(self):
        a=self.adapter();self.request.close()
        with self.assertRaises(AuthError) as caught:a.claim(UUID(uid(60)),'closed')
        self.assertEqual(caught.exception.code,'core_unavailable')
