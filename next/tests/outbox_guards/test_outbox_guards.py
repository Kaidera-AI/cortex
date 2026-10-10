"""Raw integrity regressions from the fresh C06 source audit."""
import hashlib
import sys
from pathlib import Path
from uuid import UUID
from psycopg.types.json import Jsonb
from cortex_core.auth import AuthError, authorized
from cortex_core.records import Records
from cortex_core.coordination import Jobs
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
