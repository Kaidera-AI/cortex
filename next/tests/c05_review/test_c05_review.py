"""Frozen independent C05 edge cases; uses unchanged C04 real-PG setup."""
import sys
from uuid import UUID
sys.path.insert(0, '/tmp/next/tests')
from core_db_fixture import Fixture, WRITE_A, OWNER_A, uid
from cortex_core.coordination import Jobs, JobError
from cortex_core.records import Records, RecordError


class ReviewCases(Fixture):
    def jobs(self, key=WRITE_A):
        return Jobs(self.request, key, UUID(uid(1)), UUID(uid(3)))

    def records(self):
        return Records(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3)))

    def pending_cancel(self, path):
        owner, worker, job = self.jobs(OWNER_A), self.jobs(), UUID(uid(60))
        owner.create(job, 'handoff' if path == 'rework' else 'extract', b'intent', 'create')
        attempt = worker.claim(job, 'claim')
        if path == 'fail':
            worker.fail(job, attempt.attempt_id, attempt.fence, b'failed', 'fail')
        elif path == 'release':
            worker.release(job, attempt.attempt_id, attempt.fence, 'release')
        elif path == 'expiry':
            self.admin.execute("UPDATE coordination.leases SET expires_at=clock_timestamp()-interval '1 second' WHERE resource_id=%s", (str(job),))
            owner.expire(job, 'expire')
        else:
            worker.return_result(job, attempt.attempt_id, attempt.fence, b'returned', 'return')
            owner.rework(job, attempt.attempt_id, attempt.fence, 'rework')
        before = self.admin.execute('SELECT attempt_id,outcome,payload_ref FROM coordination.job_results ORDER BY attempt_id').fetchall()
        self.assertEqual(len(before), 1)
        self.assertEqual(owner.retry(job, 'retry').state, 'pending')
        error = None
        try:
            receipt = owner.cancel(job, 'cancel')
        except JobError as caught:
            error = caught.code
        current = self.admin.execute('SELECT state,cancel_requested FROM coordination.jobs WHERE id=%s', (str(job),)).fetchone()
        after = self.admin.execute('SELECT attempt_id,outcome,payload_ref FROM coordination.job_results ORDER BY attempt_id').fetchall()
        self.assertEqual(after, before, 'owner cancellation must preserve the prior immutable result')
        self.assertIsNone(error, f'{path} -> retry -> pending cancel returned {error}; DB state={current}')
        self.assertEqual((receipt.state, current), ('canceled', ('canceled', True)))
        self.assertEqual(owner.cancel(job, 'cancel'), receipt)
        with self.assertRaises(JobError) as caught:
            worker.claim(job, 'late-claim')
        self.assertEqual(caught.exception.code, 'conflict')

    def test_cancel_pending_after_failure_retry(self):
        self.pending_cancel('fail')

    def test_cancel_pending_after_release_retry(self):
        self.pending_cancel('release')

    def test_cancel_pending_after_expiry_retry(self):
        self.pending_cancel('expiry')

    def test_cancel_pending_after_rework_retry(self):
        self.pending_cancel('rework')

    def write_only(self):
        self.admin.execute('UPDATE auth.project_grants SET permissions=%s WHERE tenant_id=%s AND project_id=%s AND principal_id=%s', (['write'], uid(2), uid(3), uid(10)))

    def test_write_only_committed_create_replays_same_receipt(self):
        self.write_only()
        adapter, record = self.records(), UUID(uid(50))
        first = adapter.put(record, 'memory', b'exact', 0, 'same-request')
        self.assertEqual(first.revision, 1)
        error = None
        try:
            second = adapter.put(record, 'memory', b'exact', 0, 'same-request')
        except RecordError as caught:
            error = caught.code
        counts = self.admin.execute('SELECT (SELECT count(*) FROM core.record_revisions WHERE record_id=%s),(SELECT count(*) FROM coordination.idempotency WHERE request_key=%s)', (str(record), 'same-request')).fetchone()
        self.assertEqual(counts, (1, 1))
        self.assertIsNone(error, f'fresh write-only authorization committed revision 1 but same-request replay returned {error}')
        self.assertEqual(second, first)

    def test_write_only_current_revision_update(self):
        adapter, record = self.records(), UUID(uid(50))
        adapter.put(record, 'memory', b'first', 0, 'create')
        self.write_only()
        error = None
        try:
            updated = adapter.put(record, 'memory', b'second', 1, 'update')
        except RecordError as caught:
            error = caught.code
        self.assertIsNone(error, f'current revision + fresh write-only grant returned {error}')
        self.assertEqual(updated.revision, 2)

    def test_positive_initial_pending_cancel(self):
        owner, job = self.jobs(OWNER_A), UUID(uid(60))
        owner.create(job, 'extract', b'intent', 'create')
        receipt = owner.cancel(job, 'cancel')
        self.assertEqual(receipt.state, 'canceled')
        self.assertEqual(owner.cancel(job, 'cancel'), receipt)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.job_results').fetchone()[0], 0)

    def test_positive_read_write_create_replay(self):
        adapter, record = self.records(), UUID(uid(50))
        first = adapter.put(record, 'memory', b'exact', 0, 'same-request')
        self.assertEqual(adapter.put(record, 'memory', b'exact', 0, 'same-request'), first)
