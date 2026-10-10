"""Frozen C04a policy integration; Core owns role and human-kind evidence."""
import inspect
import json
from pathlib import Path
import sys
from uuid import UUID

from cortex_core.auth import AuthError
from cortex_core.coordination import Jobs, JobError
from cortex_core.identity import Identity
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, uid


class IdentityCoordination(Fixture):
    def ports(self):
        self.assertIn('recipient_role', inspect.signature(Jobs.create).parameters, 'missing Core role policy')
        self.assertIn('human_review', inspect.signature(Jobs.create).parameters, 'missing Core human-review policy')
        return Identity(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)))

    def jobs(self, key=OWNER_A):
        return Jobs(self.request, key, UUID(uid(1)), UUID(uid(3)))

    def worker(self, principal=70, roles=('worker',), permissions=('read','write')):
        return self.ports().register_agent(UUID(uid(principal)), 'worker-'+str(principal), roles, permissions,
                                          request_key='identity-'+str(principal))

    def human(self, principal=71):
        return self.ports().register_human(UUID(uid(principal)), 'human-'+str(principal), ('reviewer',), ('owner',),
            owner_attestation='synthetic private owner attestation', request_key='identity-'+str(principal))

    def create(self, human_review=False, kind='handoff'):
        self.ports()
        return self.jobs().create(UUID(uid(80)), kind, b'exact\x00intent\xff', 'create',
                                  recipient_role='worker', human_review=human_review)

    def test_owner_policy_is_persisted_visible_and_bound_to_create_retry(self):
        self.create(human_review=True)
        view = self.jobs().get(UUID(uid(80)))
        self.assertEqual((view.recipient_role, view.human_review, view.body), ('worker', True, b'exact\x00intent\xff'))
        row = self.admin.execute('SELECT p.body FROM coordination.jobs j JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(j.tenant_id,j.project_id,j.payload_ref) WHERE j.id=%s', (uid(80),)).fetchone()
        meta = json.loads(bytes(row[0]))
        self.assertEqual((meta['recipient_role'], meta['human_review']), ('worker', True))
        with self.assertRaises(JobError):
            self.jobs().create(UUID(uid(80)), 'handoff', b'exact\x00intent\xff', 'create', recipient_role='reviewer', human_review=True)
        with self.assertRaises(AuthError):
            self.jobs(WRITE_A).create(UUID(uid(81)), 'handoff', b'intent', 'writer-create', recipient_role='worker', human_review=True)

    def test_only_fresh_adopted_core_role_can_claim_and_context_spoof_refuses(self):
        self.create()
        wrong = self.worker(principal=72, roles=('other-role',))
        for key in (WRITE_A, OWNER_A, wrong.secret):
            self.request.execute("SELECT set_config('cortex.roles','worker',false),set_config('cortex.principal_kind','human',false)")
            with self.subTest(principal_class='wrong-role'), self.assertRaises(JobError):
                self.jobs(key).claim(UUID(uid(80)), 'claim-wrong')
        worker = self.worker()
        claim = self.jobs(worker.secret).claim(UUID(uid(80)), 'claim')
        self.assertEqual(claim.holder, str(worker.principal_id))

    def test_role_membership_does_not_cross_project_scope(self):
        worker = self.worker()
        self.admin.execute("INSERT INTO core.projects(tenant_id,id,name) VALUES (%s,%s,'other-project')", (uid(2), uid(90)))
        self.admin.execute('INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions,roles) VALUES (%s,%s,%s,%s,%s)',
                           (uid(2), uid(90), worker.principal_id, ['read','write'], ['other-role']))
        self.admin.execute('INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions) VALUES (%s,%s,%s,%s)',
                           (uid(2), uid(90), UUID(uid(12)), ['owner']))
        owner = Jobs(self.request, OWNER_A, UUID(uid(1)), UUID(uid(90)))
        owner.create(UUID(uid(80)), 'work', b'other-project', 'other-create', recipient_role='worker')
        with self.assertRaises(JobError):
            Jobs(self.request, worker.secret, UUID(uid(1)), UUID(uid(90))).claim(UUID(uid(80)), 'other-claim')
        self.assertEqual(owner.get(UUID(uid(80))).state, 'pending')

    def test_role_withdrawal_refuses_current_worker_and_historical_claim_replay(self):
        self.create(kind='work')
        worker = self.worker()
        jobs = self.jobs(worker.secret)
        claim = jobs.claim(UUID(uid(80)), 'claim')
        self.ports().set_roles(worker.principal_id, ('other-role',), request_key='withdraw-role')
        with self.assertRaises(JobError): jobs.claim(UUID(uid(80)), 'claim')
        with self.assertRaises(JobError): jobs.complete(UUID(uid(80)), claim.attempt_id, claim.fence, b'result', 'complete')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0], 0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_attempts').fetchone()[0], 1)

    def test_human_review_uses_core_kind_not_owner_permissions_or_guc(self):
        self.create(human_review=True)
        worker = self.worker()
        claim = self.jobs(worker.secret).claim(UUID(uid(80)), 'claim')
        self.jobs(worker.secret).return_result(UUID(uid(80)), claim.attempt_id, claim.fence, b'returned', 'return')
        agent_owner = self.worker(principal=72, roles=('human','reviewer'), permissions=('owner',))
        self.request.execute("SELECT set_config('cortex.principal_kind','human',false)")
        for key in (OWNER_A, agent_owner.secret):
            with self.subTest(kind_class='unadopted-or-agent'), self.assertRaises(JobError):
                self.jobs(key).accept(UUID(uid(80)), claim.attempt_id, claim.fence, 'wrong-kind')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0], 0)

    def test_owner_attested_human_accepts_exact_return_and_replay_rechecks_kind(self):
        self.create(human_review=True)
        worker = self.worker(); human = self.human()
        claim = self.jobs(worker.secret).claim(UUID(uid(80)), 'claim')
        self.jobs(worker.secret).return_result(UUID(uid(80)), claim.attempt_id, claim.fence, b'exact\x00return\xff', 'return')
        result = self.jobs(human.secret).accept(UUID(uid(80)), claim.attempt_id, claim.fence, 'accept')
        self.assertEqual(result.state, 'succeeded')
        body = self.admin.execute('SELECT p.body FROM coordination.job_results r JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(r.tenant_id,r.project_id,r.payload_ref)').fetchone()[0]
        self.assertEqual(bytes(body), b'exact\x00return\xff')
        self.admin.execute("UPDATE auth.principals SET kind='agent' WHERE tenant_id=%s AND id=%s", (uid(2), human.principal_id))
        with self.assertRaises(JobError):
            self.jobs(human.secret).accept(UUID(uid(80)), claim.attempt_id, claim.fence, 'accept')

    def test_human_kind_never_bypasses_independent_review(self):
        self.create(human_review=True)
        human = self.human()
        self.ports().set_roles(human.principal_id, ('worker',), request_key='human-worker-role')
        jobs = self.jobs(human.secret)
        claim = jobs.claim(UUID(uid(80)), 'claim')
        jobs.return_result(UUID(uid(80)), claim.attempt_id, claim.fence, b'self-return', 'return')
        with self.assertRaises(JobError): jobs.accept(UUID(uid(80)), claim.attempt_id, claim.fence, 'self-accept')
        with self.assertRaises(JobError): jobs.rework(UUID(uid(80)), claim.attempt_id, claim.fence, 'self-rework')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0], 0)

    def test_invalid_policy_refuses_before_partial_job_or_payload_writes(self):
        self.ports()
        before = self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        for policy in ({'recipient_role':'bad\nrole'}, {'recipient_role': True}, {'human_review':1},
                       {'human_review':True, 'kind':'work'}):
            args = dict(policy); kind = args.pop('kind', 'handoff')
            with self.subTest(policy_class='invalid'), self.assertRaises(JobError):
                self.jobs().create(UUID(uid(80)), kind, b'intent', 'invalid-policy', **args)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.jobs').fetchone()[0], 0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0], before)
