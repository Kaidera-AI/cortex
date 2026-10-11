"""Policy-free creation preserves the published C05 durable request format."""
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID

from cortex_core.coordination import Jobs
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, uid


class IdentityTransition(Fixture):
    def test_policy_free_create_keeps_c05_request_digest_and_replay(self):
        job_id = UUID(uid(80)); body = b'published-C05\x00intent\xff'
        jobs = Jobs(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)))
        result = jobs.create(job_id, 'work', body, 'published-c05-create')
        # Format from published C05 5f43f43, before the optional policy extension.
        digest = hashlib.sha256(json.dumps(['job.create',str(job_id),
            ['work',hashlib.sha256(body).hexdigest(),None]], separators=(',',':')).encode()).hexdigest()
        actual = self.admin.execute('SELECT request_sha256 FROM coordination.idempotency WHERE tenant_id=%s AND project_id=%s AND principal_id=%s AND request_key=%s',
            (uid(2),uid(3),uid(12),'published-c05-create')).fetchone()[0]
        self.assertEqual(actual, digest, 'optional policy defaults must retain the published durable request format')
        self.assertEqual(jobs.create(job_id, 'work', body, 'published-c05-create'), result)
        self.assertEqual(jobs.get(job_id).body, body)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.jobs WHERE id=%s',(job_id,)).fetchone()[0],1)
