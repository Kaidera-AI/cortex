"""Deterministic protection creation races at the real publication lock."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from queue import Queue
import sys
import time
from uuid import UUID
import psycopg
from cortex_core.outbox import Outbox
from cortex_core.records import Records
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, API, REQUEST, uid


class RetentionProtocol(Fixture):
    def port(self,connection=None):return Outbox(connection or self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
    def seed(self):
        r=Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3))).put(UUID(uid(50)),'memory',b'protected original bytes',0,'create')
        self.port().publish();self.admin.execute("UPDATE coordination.published_events SET published_at=clock_timestamp()-interval '8 days'")
        return r.event_id

    def wait_block(self,pid,blocker=None):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            rows=self.admin.execute('SELECT pg_blocking_pids(%s)',(pid,)).fetchone()[0]
            if rows and (blocker is None or blocker in rows):return
            time.sleep(.01)
        raise RuntimeError('required PostgreSQL lock synchronization was not observed')

    def race(self,kind):
        event=self.seed();pruner=Queue();protector=Queue()
        def prune():
            with psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',API+'@'),autocommit=True) as c:
                c.execute('SET ROLE "'+REQUEST+'"');pruner.put(c.info.backend_pid)
                return self.port(c).prune()
        def protect():
            with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as c:
                protector.put(c.info.backend_pid)
                try:
                    if kind=='snapshot':
                        c.execute("INSERT INTO coordination.snapshot_floors VALUES(%s,%s,'rebuild',0,clock_timestamp()+interval '1 hour')",(uid(1),uid(300)))
                    else:
                        c.execute("INSERT INTO coordination.quarantine(tenant_id,project_id,event_id,module_id,error_code) VALUES(%s,%s,%s,'graph','poison')",(uid(2),uid(3),event))
                    return 'committed'
                except psycopg.Error:return 'refused'
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.admin.transaction():
                self.admin.execute('SELECT cursor FROM coordination.published_events FOR UPDATE')
                pruning=pool.submit(prune);pid=pruner.get(timeout=5)
                self.wait_block(pid,self.admin.info.backend_pid)
                protection=pool.submit(protect);other=protector.get(timeout=5)
                deadline=time.monotonic()+5
                while not protection.done():
                    if self.admin.execute('SELECT pg_blocking_pids(%s)',(other,)).fetchone()[0]:break
                    if time.monotonic()>deadline:raise RuntimeError('protection neither committed nor waited for publication lock')
                    time.sleep(.01)
            pruning.result(timeout=10);result=protection.result(timeout=10)
        remains=self.admin.execute('SELECT count(*) FROM coordination.outbox WHERE event_id=%s',(event,)).fetchone()[0]
        self.assertTrue(result=='refused' or remains==1,'a committed protection was bypassed by concurrent pruning')

    def test_snapshot_creation_serializes_with_actual_pruning(self):self.race('snapshot')
    def test_quarantine_creation_serializes_with_actual_pruning(self):self.race('quarantine')

    def test_active_checkpoint_cannot_be_created_below_retained_floor(self):
        self.seed();self.port().prune()
        with self.assertRaises(psycopg.Error):
            self.admin.execute("INSERT INTO coordination.consumer_checkpoints(installation_id,module_id,applied_cursor,state) VALUES(%s,'graph',0,'active')",(uid(1),))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.consumer_checkpoints').fetchone()[0],0)

    def test_expired_checkpoint_cannot_advance_or_reactivate_completeness(self):
        self.seed()
        self.admin.execute("INSERT INTO coordination.consumer_checkpoints(installation_id,module_id,applied_cursor,state,last_seen_at,expires_at) VALUES(%s,'graph',0,'expired',clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')",(uid(1),))
        with self.assertRaises(psycopg.Error):
            self.admin.execute("UPDATE coordination.consumer_checkpoints SET applied_cursor=1,state='active' WHERE module_id='graph'")
        self.assertEqual(self.admin.execute("SELECT applied_cursor,state FROM coordination.consumer_checkpoints WHERE module_id='graph'").fetchone(),(0,'expired'))
