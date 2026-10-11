"""C05 late lease/input regressions, frozen before the guard repair."""
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import UUID
import psycopg
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture,WRITE_A,OWNER_A,uid
from cortex_core import coordination as jobs,records

class AcceptanceGuards(Fixture):
    def port(self,key=WRITE_A):return jobs.Jobs(self.request,key,UUID(uid(1)),UUID(uid(3)))
    def create(self,kind='extract'):
        self.port(OWNER_A).create(UUID(uid(90)),kind,b'intent','create')
        return self.port().claim(UUID(uid(90)),'claim',ttl_seconds=1)
    def delay_result(self):
        real=jobs._append_result
        def delay(*args,**kwargs):
            real(*args,**kwargs)
            args[0].execute('SELECT pg_sleep(1.2)')
        return patch.object(jobs,'_append_result',side_effect=delay)
    def assert_invalid(self,call,kind):
        try:call()
        except Exception as error:
            self.assertIsInstance(error,kind)
            self.assertEqual(error.code,'invalid_input')
        else:self.fail('unsupported input accepted')
    def test_expiry_after_real_result_insert_refuses_and_rolls_back(self):
        a=self.create();before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        with self.delay_result(),self.assertRaises(jobs.JobError) as caught:
            self.port().complete(a.job_id,a.attempt_id,a.fence,b'late','late')
        self.assertEqual(caught.exception.code,'conflict')
        self.assertEqual(self.port().get(a.job_id).state,'running')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],before)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.idempotency WHERE request_key='late'").fetchone()[0],0)
    def test_expiry_during_independent_accept_refuses_and_rolls_back(self):
        a=self.create('handoff');self.port().return_result(a.job_id,a.attempt_id,a.fence,b'handback','return')
        with self.delay_result(),self.assertRaises(jobs.JobError) as caught:
            self.port(OWNER_A).accept(a.job_id,a.attempt_id,a.fence,'accept')
        self.assertEqual(caught.exception.code,'conflict');self.assertEqual(self.port().get(a.job_id).state,'running')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
    def test_expiry_after_actual_claim_attempt_refuses_and_rolls_back(self):
        self.port(OWNER_A).create(UUID(uid(90)),'extract',b'intent','create')
        real=psycopg.Connection.execute
        def delayed(connection,query,*args,**kwargs):
            result=real(connection,query,*args,**kwargs)
            if connection is self.request and isinstance(query,str) and 'INSERT INTO coordination.job_attempts' in query:
                real(connection,'SELECT pg_sleep(1.2)')
            return result
        with patch.object(psycopg.Connection,'execute',new=delayed),self.assertRaises(jobs.JobError) as caught:
            self.port().claim(UUID(uid(90)),'claim',ttl_seconds=1)
        self.assertEqual(caught.exception.code,'conflict');self.assertEqual(self.port().get(UUID(uid(90))).state,'pending')
        for table in ('coordination.job_attempts','coordination.leases'):
            self.assertEqual(self.admin.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
    def test_fence_types_refuse_before_encoding_in_every_worker_and_review_port(self):
        a=self.create()
        for fence in (object(),float('nan'),2**63,True):
            for method in ('complete','fail','return_result'):
                with self.subTest(method=method,kind=type(fence).__name__):
                    self.assert_invalid(lambda method=method,fence=fence:getattr(self.port(),method)(a.job_id,a.attempt_id,fence,b'body','invalid'),jobs.JobError)
            for method in ('release','abandon','accept','rework'):
                with self.subTest(method=method,kind=type(fence).__name__):
                    self.assert_invalid(lambda method=method,fence=fence:getattr(self.port(OWNER_A),method)(a.job_id,a.attempt_id,fence,'invalid'),jobs.JobError)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
    def test_surrogate_request_key_has_typed_refusal_without_partial_rows(self):
        a=records.Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)))
        before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        self.assert_invalid(lambda:a.put(UUID(uid(90)),'memory',b'body',0,'invalid\ud800'),records.RecordError)
        self.assert_invalid(lambda:self.port(OWNER_A).create(UUID(uid(90)),'extract',b'body','invalid\ud800'),jobs.JobError)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],before)
    def test_record_body_over_private_limit_is_typed_and_atomic(self):
        a=records.Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)))
        before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        self.assert_invalid(lambda:a.put(UUID(uid(90)),'memory',b'x'*(16*1024*1024+1),0,'oversize'),records.RecordError)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],before)
    def test_job_intent_and_all_result_paths_share_private_byte_bound(self):
        body=b'x'*(16*1024*1024+1);before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        with self.subTest(operation='create'):
            self.assert_invalid(lambda:self.port(OWNER_A).create(UUID(uid(91)),'extract',body,'oversize'),jobs.JobError)
        a=self.create('handoff');after_create=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        for method in ('complete','fail','return_result'):
            with self.subTest(operation=method):
                self.assert_invalid(lambda method=method:getattr(self.port(),method)(a.job_id,a.attempt_id,a.fence,body,'oversize-'+method),jobs.JobError)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],after_create)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0],0)
