"""C06 reserved Core fact integrity under a write-only record mutation context."""
import sys
from pathlib import Path
from uuid import UUID
from cortex_core.auth import AuthError
from cortex_core.coordination import Jobs
from cortex_core.identity import Identity
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, uid

class PrivateNamespaceCases(Fixture):
    def refuse_revision(self,kind,target):
        aggregate,revision,payload=self.admin.execute('''SELECT a.record_id,r.current_revision,v.payload_ref
          FROM core.record_aliases a JOIN core.records r
          ON(r.tenant_id,r.project_id,r.id)=(a.tenant_id,a.project_id,a.record_id)
          JOIN core.record_revisions v ON(v.tenant_id,v.project_id,v.record_id,v.revision)
          =(r.tenant_id,r.project_id,r.id,r.current_revision)
          WHERE a.source_namespace=%s AND a.external_id=%s''',('cortex.core.'+kind,str(target))).fetchone()
        before=self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0]
        self.admin.execute('UPDATE auth.project_grants SET permissions=%s WHERE tenant_id=%s AND project_id=%s AND principal_id=%s',(['write'],uid(2),uid(3),uid(10)))
        rejected=False
        try:
            with self.auth(WRITE_A,action='write'):
                self.request.execute('SELECT coordination.c05_update_head(%s,%s,%s,true)',(aggregate,revision,revision+1))
                self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,%s,%s,true)',(uid(2),uid(3),aggregate,revision+1,payload))
        except AuthError as caught:
            self.assertEqual(caught.code,'core_unavailable');rejected=True
        self.assertTrue(rejected,'reserved Core fact must reject an ordinary write-only revision')
        self.assertEqual(self.admin.execute('SELECT current_revision,tombstone FROM core.records WHERE id=%s',(aggregate,)).fetchone(),(revision,False))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(aggregate,)).fetchone()[0],revision)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0],before)

    def test_write_only_raw_job_fact_revision_refuses_without_extra_event(self):
        target=UUID(uid(60))
        jobs=Jobs(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        jobs.create(target,'work',b'intent','create')
        self.refuse_revision('job',target)
        self.assertEqual(jobs.get(target).state,'pending')

    def test_write_only_raw_identity_fact_revision_refuses_without_extra_event(self):
        target=UUID(uid(70))
        identity=Identity(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        identity.register_agent(target,'worker',('worker',),('read',),request_key='register')
        self.refuse_revision('identity',target)
        self.assertEqual(identity.lookup(target).kind,'agent')
