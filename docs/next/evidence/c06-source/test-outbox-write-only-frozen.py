"""C06 write-only acknowledgement and private current-transaction event boundary."""
import sys
from uuid import UUID
import psycopg
sys.path.insert(0,'/tmp/next/tests')
from core_db_fixture import Fixture, WRITE_A, uid
from cortex_core.records import Records,RecordError
from cortex_core.auth import AuthError

class WriteOnlyCases(Fixture):
    def adapter(self):return Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)))

    def test_write_only_mutations_ack_and_replay_exact_current_events(self):
        self.admin.execute('UPDATE auth.project_grants SET permissions=%s WHERE tenant_id=%s AND project_id=%s AND principal_id=%s', (['write'],uid(2),uid(3),uid(10)))
        adapter=self.adapter();record=UUID(uid(50));receipts=[]
        for operation in ('create','update','delete'):
            error=None
            try:
                if operation=='delete':result=adapter.delete(record,2,operation)
                else:result=adapter.put(record,'memory',operation.encode(),0 if operation=='create' else 1,operation)
            except (RecordError,AuthError) as caught:error=caught.code
            self.assertIsNone(error,'fresh write authority must receive its current canonical event acknowledgement')
            replay=adapter.delete(record,2,operation) if operation=='delete' else adapter.put(record,'memory',operation.encode(),0 if operation=='create' else 1,operation)
            self.assertEqual(replay,result)
            self.assertIsInstance(result.event_id,UUID);receipts.append(result)
        self.assertEqual([r.revision for r in receipts],[1,2,3])
        self.assertEqual(len({r.event_id for r in receipts}),3)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox WHERE aggregate_id=%s',(record,)).fetchone()[0],3)
        with self.assertRaises(AuthError) as caught:adapter.get(record)
        self.assertEqual(caught.exception.code,'forbidden')

    def test_private_event_lookup_requires_bound_write_and_current_transaction(self):
        self.assertIsNotNone(self.admin.execute("SELECT to_regprocedure('coordination.c06_record_event(uuid,bigint)')").fetchone()[0],'private event entry point absent')
        record=UUID(uid(50));self.adapter().put(record,'memory',b'own',0,'create')
        query='SELECT coordination.c06_record_event(%s,1)';args=(record,)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):self.request.execute(query,args)
        with self.auth(WRITE_A,action='read'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():self.request.execute(query,args)
        with self.auth(WRITE_A,action='write'):
            self.assertIsNone(self.request.execute(query,args).fetchone()[0],'a prior committed event cannot become the current write acknowledgement')
