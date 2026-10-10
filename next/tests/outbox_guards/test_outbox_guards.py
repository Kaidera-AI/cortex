"""Raw integrity regressions from the fresh C06 source audit."""
import hashlib
import json
import sys
from pathlib import Path
from uuid import UUID
from psycopg.types.json import Jsonb
from cortex_core.auth import AuthError, authorized
from cortex_core.records import Records, RecordError
from cortex_core.outbox import Outbox
from cortex_core.coordination import Jobs, JobError
from cortex_core.identity import Identity
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, uid


class OutboxGuards(Fixture):
    def records(self):return Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)))
    def jobs(self,key=OWNER_A):return Jobs(self.request,key,UUID(uid(1)),UUID(uid(3)))
    def identity(self):return Identity(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
    def write(self):return authorized(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)),'write')
    def count(self):return self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0]

    def test_raw_record_head_rewind_refuses_without_eventless_state(self):
        self.records().put(UUID(uid(50)),'memory',b'first',0,'first')
        self.records().put(UUID(uid(50)),'memory',b'second',1,'second')
        with self.assertRaises(AuthError):
            with self.write():self.request.execute('UPDATE core.records SET current_revision=1 WHERE id=%s',(uid(50),))
        self.assertEqual((self.records().get(UUID(uid(50))).revision,self.count()),(2,2))

    def test_history_without_matching_head_cannot_commit_extra_event(self):
        self.records().put(UUID(uid(50)),'memory',b'first',0,'first');body=b'unheaded history'
        with self.assertRaises(AuthError):
            with self.write():
                self.request.execute('INSERT INTO core.payloads VALUES(%s,%s,%s,%s,%s)',(uid(2),uid(3),uid(150),body,hashlib.sha256(body).hexdigest()))
                self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,2,%s,false)',(uid(2),uid(3),uid(50),uid(150)))
        self.assertEqual((self.records().get(UUID(uid(50))).revision,self.count()),(1,1))

    def test_capture_before_later_job_change_cannot_satisfy_parent_guard(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create')
        with self.assertRaises(AuthError):
            with self.write():
                self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),))
                self.request.execute("UPDATE coordination.jobs SET state='canceled' WHERE id=%s",(uid(60),))
        self.assertEqual((self.jobs().get(UUID(uid(60))).state,self.count()),('pending',1))

    def test_capture_then_second_job_change_cannot_leave_stale_snapshot(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create')
        with self.assertRaises(AuthError):
            with self.write():
                self.request.execute("UPDATE coordination.jobs SET state='running' WHERE id=%s",(uid(60),))
                self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),))
                self.request.execute("UPDATE coordination.jobs SET state='canceled' WHERE id=%s",(uid(60),))
        self.assertEqual((self.jobs().get(UUID(uid(60))).state,self.count()),('pending',1))

    def test_capture_without_any_business_change_refuses_duplicate_fact(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create')
        with self.assertRaises(AuthError):
            with self.write():self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),))
        self.assertEqual(self.count(),1)

    def test_job_lease_kind_cannot_be_changed_to_bypass_parent_guard(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');self.jobs(WRITE_A).claim(UUID(uid(60)),'claim')
        with self.assertRaises(AuthError):
            with self.write():self.request.execute("UPDATE coordination.leases SET kind='other' WHERE kind='job' AND resource_id=%s",(uid(60),))
        self.assertEqual((self.jobs().get(UUID(uid(60))).state,self.count()),('running',2))
        self.assertEqual(self.admin.execute('SELECT kind FROM coordination.leases').fetchone()[0],'job')

    def test_role_change_receipt_exposes_committed_identity_event(self):
        self.identity().register_agent(UUID(uid(70)),'worker',('worker',),('read',),request_key='register')
        result=self.identity().set_roles(UUID(uid(70)),('reviewer',),request_key='roles')
        self.assertTrue(hasattr(result,'event_id'),'role receipt lacks committed event identity')
        self.assertEqual(result.event_id,self.admin.execute('SELECT event_id FROM coordination.outbox ORDER BY occurred_at DESC LIMIT 1').fetchone()[0])

    def test_role_change_replay_returns_original_receipt_after_later_change(self):
        self.identity().register_agent(UUID(uid(70)),'worker',('worker',),('read',),request_key='register')
        first=self.identity().set_roles(UUID(uid(70)),('reviewer',),request_key='first')
        self.identity().set_roles(UUID(uid(70)),('worker',),request_key='second')
        replay=self.identity().set_roles(UUID(uid(70)),('reviewer',),request_key='first')
        self.assertEqual((replay.roles,replay.generation),(first.roles,first.generation));self.assertEqual(self.count(),3)

    def test_raw_receipt_tampering_cannot_change_replayed_event_id(self):
        original=self.records().put(UUID(uid(50)),'memory',b'first',0,'first')
        with self.assertRaises(AuthError):
            with self.write():self.request.execute("UPDATE coordination.idempotency SET receipt=jsonb_set(receipt,'{event_id}',%s::jsonb) WHERE request_key='first'",(Jsonb(uid(99)),))
        self.assertEqual(self.records().put(UUID(uid(50)),'memory',b'first',0,'first').event_id,original.event_id)
        self.assertEqual(self.count(),1)

    def test_raw_pre_forged_receipt_without_current_mutation_refuses(self):
        with self.assertRaises(AuthError):
            with self.write():self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",
                (uid(2),uid(3),uid(10),'a'*64,Jsonb(dict(record_id=uid(50),revision=1,tombstone=False,payload_sha256='b'*64,event_id=uid(99)))))
        self.assertEqual(self.count(),0)

    def test_reverse_history_insertion_refuses_revision_reordering(self):
        self.records().put(UUID(uid(50)),'memory',b'first',0,'first');rejected=False
        try:
            with self.write():
                for revision in (2,3):
                    self.request.execute('UPDATE core.records SET current_revision=%s WHERE id=%s',(revision,uid(50)))
                for revision in (3,2):
                    body=('revision-'+str(revision)).encode()
                    self.request.execute('INSERT INTO core.payloads VALUES(%s,%s,%s,%s,%s)',
                        (uid(2),uid(3),uid(150+revision),body,hashlib.sha256(body).hexdigest()))
                    self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,%s,%s,false)',
                        (uid(2),uid(3),uid(50),revision,uid(150+revision)))
        except AuthError:
            rejected=True
        port=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)));port.publish()
        revisions=[event.envelope['aggregate_revision'] for event in port.page().events]
        self.assertTrue(rejected,'reverse history admitted; published revisions='+str(revisions))
        self.assertEqual(revisions,[1]);self.assertEqual((self.records().get(UUID(uid(50))).revision,self.count()),(1,1))

    def test_authentic_pending_event_cannot_forge_successful_completion_receipt(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');rejected=False
        args=[uid(99),1,hashlib.sha256(b'x').hexdigest()]
        digest=hashlib.sha256(json.dumps(['job.complete',uid(60),args],separators=(',',':')).encode()).hexdigest()
        try:
            with self.write():
                self.request.execute('UPDATE coordination.jobs SET state=state WHERE id=%s',(uid(60),))
                event=self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),)).fetchone()[0]
                receipt=dict(type='job',job_id=uid(60),state='succeeded',reason='completed',event_id=str(event),request_json=json.dumps(['job.complete',uid(60),args],separators=(',',':')))
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",
                    (uid(2),uid(3),uid(10),digest,Jsonb(receipt)))
        except AuthError:
            rejected=True
        observed=None if rejected else self.jobs(WRITE_A).complete(UUID(uid(60)),UUID(uid(99)),1,b'x','forged').state
        self.assertTrue(rejected,'forged completion replay='+str(observed)+'; actual job='+self.jobs().get(UUID(uid(60))).state)
        self.assertEqual((self.jobs().get(UUID(uid(60))).state,self.count()),('pending',1))

    def test_authentic_running_event_cannot_forge_claim_attempt_fence_or_holder(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');original=self.jobs(WRITE_A).claim(UUID(uid(60)),'claim');rejected=False
        digest=hashlib.sha256(json.dumps(['job.claim',uid(60),[60]],separators=(',',':')).encode()).hexdigest()
        try:
            with self.write():
                self.request.execute('UPDATE coordination.jobs SET state=state WHERE id=%s',(uid(60),))
                event=self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),)).fetchone()[0]
                receipt=dict(type='claim',job_id=uid(60),attempt_id=uid(99),attempt_number=999,fence=999,holder=uid(99),event_id=str(event),request_json=json.dumps(['job.claim',uid(60),[60]],separators=(',',':')))
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",
                    (uid(2),uid(3),uid(10),digest,Jsonb(receipt)))
        except AuthError:
            rejected=True
        observed=None if rejected else self.jobs(WRITE_A).claim(UUID(uid(60)),'forged')
        self.assertTrue(rejected,'forged claim replay='+str(observed))
        self.assertEqual(self.jobs(WRITE_A).claim(UUID(uid(60)),'claim'),original);self.assertEqual(self.count(),2)

    def test_authentic_upsert_receipt_cannot_ack_a_future_delete(self):
        self.records().put(UUID(uid(50)),'memory',b'first',0,'first');rejected=False;body=b'new'
        digest=hashlib.sha256(json.dumps(['delete',uid(50),None,None,1],separators=(',',':')).encode()).hexdigest()
        try:
            with self.write():
                self.request.execute('UPDATE core.records SET current_revision=2 WHERE id=%s',(uid(50),))
                self.request.execute('INSERT INTO core.payloads VALUES(%s,%s,%s,%s,%s)',(uid(2),uid(3),uid(150),body,hashlib.sha256(body).hexdigest()))
                self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,2,%s,false)',(uid(2),uid(3),uid(50),uid(150)))
                event=self.request.execute('SELECT event_id FROM coordination.outbox WHERE aggregate_id=%s AND aggregate_revision=2',(uid(50),)).fetchone()[0]
                receipt=dict(record_id=uid(50),revision=2,tombstone=False,payload_sha256=hashlib.sha256(body).hexdigest(),event_id=str(event),request_json=json.dumps(['delete',uid(50),None,None,1],separators=(',',':')))
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",(uid(2),uid(3),uid(10),digest,Jsonb(receipt)))
        except AuthError:
            rejected=True
        if not rejected:
            with self.assertRaises(RecordError):self.records().delete(UUID(uid(50)),1,'forged')
        self.assertIsNotNone(self.records().get(UUID(uid(50))))

    def test_authentic_pending_receipt_cannot_ack_a_future_retry(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');rejected=False
        digest=hashlib.sha256(json.dumps(['job.retry',uid(60),[]],separators=(',',':')).encode()).hexdigest()
        try:
            with authorized(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)),'write'):
                self.request.execute('UPDATE coordination.jobs SET state=state WHERE id=%s',(uid(60),))
                event=self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),)).fetchone()[0]
                receipt=dict(type='job',job_id=uid(60),state='pending',reason='created',event_id=str(event),request_json=json.dumps(['job.retry',uid(60),[]],separators=(',',':')))
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",(uid(2),uid(3),uid(12),digest,Jsonb(receipt)))
        except AuthError:
            rejected=True
        if not rejected:
            with self.assertRaises(JobError):self.jobs().retry(UUID(uid(60)),'forged')
        self.assertEqual(self.jobs().get(UUID(uid(60))).state,'pending')

    def test_job_authoritative_id_cannot_be_relocated_without_old_parent_event(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create')
        with self.assertRaises(AuthError):
            with self.write():
                self.request.execute('UPDATE coordination.jobs SET id=%s WHERE id=%s',(uid(61),uid(60)))
                self.request.execute('SELECT coordination.capture_job(%s)',(uid(61),))
        self.assertIsNotNone(self.jobs().get(UUID(uid(60))));self.assertIsNone(self.jobs().get(UUID(uid(61))));self.assertEqual(self.count(),1)

    def test_attempt_cannot_be_reparented_without_old_job_event(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');claim=self.jobs(WRITE_A).claim(UUID(uid(60)),'claim')
        self.jobs().create(UUID(uid(61)),'work',b'intent','second')
        with self.assertRaises(AuthError):
            with self.write():
                self.request.execute('UPDATE coordination.job_attempts SET job_id=%s WHERE id=%s',(uid(61),claim.attempt_id))
                self.request.execute('UPDATE coordination.jobs SET state=state WHERE id=%s',(uid(61),))
                self.request.execute('SELECT coordination.capture_job(%s)',(uid(61),))
        self.assertEqual(self.admin.execute('SELECT job_id FROM coordination.job_attempts WHERE id=%s',(claim.attempt_id,)).fetchone()[0],UUID(uid(60)))
        self.assertEqual(self.count(),3)

    def test_job_fact_cannot_be_replayed_as_ordinary_memory_put(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');rejected=False;aggregate=None;body=None
        try:
            with self.write():
                self.request.execute('UPDATE coordination.jobs SET state=state WHERE id=%s',(uid(60),))
                event=self.request.execute('SELECT coordination.capture_job(%s)',(uid(60),)).fetchone()[0]
                aggregate,revision,body,digest=self.request.execute('SELECT o.aggregate_id,o.aggregate_revision,p.body,p.sha256 FROM coordination.outbox o JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(o.tenant_id,o.project_id,o.payload_ref) WHERE o.event_id=%s',(event,)).fetchone()
                contract=json.dumps(['put',str(aggregate),'memory',digest,revision-1],separators=(',',':'))
                receipt=dict(record_id=str(aggregate),revision=revision,tombstone=False,payload_sha256=digest,event_id=str(event),request_json=contract)
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",(uid(2),uid(3),uid(10),hashlib.sha256(contract.encode()).hexdigest(),Jsonb(receipt)))
        except AuthError:
            rejected=True
        if not rejected:
            with self.assertRaises(RecordError):self.records().put(aggregate,'memory',body,revision-1,'forged')
        self.assertEqual(self.jobs().get(UUID(uid(60))).state,'pending')

    def test_mixed_record_and_claim_receipt_cannot_bypass_job_contract(self):
        self.jobs().create(UUID(uid(60)),'work',b'intent','create');self.records().put(UUID(uid(50)),'memory',b'first',0,'first');rejected=False;body=b'new'
        contract=json.dumps(['job.claim',uid(60),[60]],separators=(',',':'))
        try:
            with self.write():
                self.request.execute('UPDATE core.records SET current_revision=2 WHERE id=%s',(uid(50),))
                self.request.execute('INSERT INTO core.payloads VALUES(%s,%s,%s,%s,%s)',(uid(2),uid(3),uid(150),body,hashlib.sha256(body).hexdigest()))
                self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,2,%s,false)',(uid(2),uid(3),uid(50),uid(150)))
                event=self.request.execute('SELECT event_id FROM coordination.outbox WHERE aggregate_id=%s AND aggregate_revision=2',(uid(50),)).fetchone()[0]
                receipt=dict(record_id=uid(50),revision=2,tombstone=False,payload_sha256=hashlib.sha256(body).hexdigest(),event_id=str(event),request_json=contract,
                    type='claim',job_id=uid(60),attempt_id=uid(99),attempt_number=999,fence=999,holder=uid(99))
                self.request.execute("INSERT INTO coordination.idempotency VALUES(%s,%s,%s,'forged',%s,'committed',%s)",(uid(2),uid(3),uid(10),hashlib.sha256(contract.encode()).hexdigest(),Jsonb(receipt)))
        except AuthError:
            rejected=True
        if not rejected:
            with self.assertRaises(JobError):self.jobs(WRITE_A).claim(UUID(uid(60)),'forged')
