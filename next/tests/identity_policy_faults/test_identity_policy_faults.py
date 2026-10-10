"""Single-effect role/persistence guards; frozen integration8 stays unchanged."""
import json
from pathlib import Path
import sys
from uuid import UUID

from cortex_core.coordination import JobError
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'identity_coordination'))
from test_identity_coordination import IdentityCoordination
from core_db_fixture import Fixture, uid


class IdentityPolicyFaults(Fixture):
    ports = IdentityCoordination.ports
    jobs = IdentityCoordination.jobs
    worker = IdentityCoordination.worker
    create = IdentityCoordination.create

    def test_wrong_adopted_role_cannot_create_a_claim_or_attempt(self):
        self.create(kind='work')
        wrong = self.worker(principal=72, roles=('other-role',))
        with self.assertRaises(JobError):
            self.jobs(wrong.secret).claim(UUID(uid(80)), 'wrong-role-claim')
        self.assertEqual(self.jobs().get(UUID(uid(80))).state, 'pending')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_attempts').fetchone()[0], 0)

    def test_policy_persistence_retains_exact_role_and_human_flag(self):
        self.create(human_review=True)
        view = self.jobs().get(UUID(uid(80)))
        self.assertEqual((view.recipient_role,view.human_review,view.body), ('worker',True,b'exact\x00intent\xff'))
        row = self.admin.execute('SELECT p.body FROM coordination.jobs j JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(j.tenant_id,j.project_id,j.payload_ref) WHERE j.id=%s', (uid(80),)).fetchone()
        metadata = json.loads(bytes(row[0]))
        self.assertEqual((metadata.get('recipient_role'),metadata.get('human_review')), ('worker',True))
