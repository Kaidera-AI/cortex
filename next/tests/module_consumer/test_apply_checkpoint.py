"""C07 frozen durable target-before-checkpoint, duplicate and project cursor controls."""
import os
from uuid import UUID
import psycopg
from cortex_core.outbox import Outbox
from common import ConsumerFixture, OWNER_A, uid


class ApplyCheckpoint(ConsumerFixture):
    def test_crash_before_apply_keeps_checkpoint(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0);self.sink.fail_before=True
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertIsNone(self.target());self.assertEqual(self.checkpoint()[1],0)

    def test_crash_after_durable_apply_replays_once(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0);self.sink.fail_after_once=True
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual(self.target()[5],1);self.assertEqual(self.checkpoint()[1],0)
        p.cycle();self.assertEqual(self.target()[5],1);self.assertEqual(self.checkpoint()[1],1)

    def test_crash_after_core_outcome_is_duplicate_safe(self):
        self.seed();raised=[False]
        def crash():
            if not raised[0]:raised[0]=True;raise TimeoutError('synthetic_after_core_commit')
        p=self.port(after_checkpoint=crash);p.register(snapshot_cursor=0)
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual((self.target()[5],self.checkpoint()[1]),(1,1))
        p.cycle();self.assertEqual((self.target()[5],self.checkpoint()[1]),(1,1))

    def test_duplicate_equal_revision_same_event_is_idempotent(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0);p.cycle();p.cycle()
        self.assertEqual((self.target()[0],self.target()[5]),(1,1))

    def test_equal_revision_different_digest_is_quarantined(self):
        self.seed();self.admin.execute('''INSERT INTO public.c07_target VALUES
            ('graph',%s,%s,1,1,%s,%s,false,%s,1)''',(uid(3),uid(50),uid(99),'0'*64,b'wrong'))
        p=self.port();p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual(self.checkpoint()[1],0);self.assertEqual(len(self.quarantine()),1)
        self.assertEqual(self.target()[1],UUID(uid(99)))

    def test_stale_upsert_cannot_resurrect_tombstone(self):
        self.seed();first=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).page().events[0]
        self.seed(body=b'new',revision=1);self.sink.fail_before=False
        from cortex_core.records import Records
        Records(self.request,b'synthetic-only-write-a',UUID(uid(1)),UUID(uid(3))).delete(UUID(uid(50)),2,'c07-delete')
        Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).publish()
        p=self.port();p.register(snapshot_cursor=0);p.cycle()
        before=self.target();self.sink.apply(first,1);after=self.target()
        self.assertTrue(after[3]);self.assertEqual(after,before)

    def test_newer_revision_applies_in_order(self):
        self.seed();self.seed(body=b'second',revision=1)
        p=self.port();p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual((self.target()[0],self.target()[5],self.checkpoint()[1]),(2,2,2))

    def test_full_project_page_does_not_jump_global_head(self):
        self.seed();self.foreign_event(cursor=2)
        p=self.port();p.register(snapshot_cursor=0);p.cycle(limit=1)
        self.assertEqual(self.checkpoint()[0],1)
        self.assertEqual(self.checkpoint()[1],1)

    def test_empty_project_page_may_advance_to_observed_head(self):
        self.foreign_event(cursor=1);p=self.port();p.register(snapshot_cursor=0);p.cycle(limit=100)
        self.assertEqual(self.checkpoint()[0],1)
        self.assertIsNone(self.target())

    def test_second_runner_refused(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0)
        other=psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@','kaidera-test-core-api@'),autocommit=True)
        self.addCleanup(other.close);other.execute('SET ROLE "kaidera-runtime-core-request"')
        with p.hold_lock():
            with self.assertRaises(self.module().ConsumerError):
                self.port(connection=other).cycle()
        self.assertEqual(self.checkpoint()[1],0)

    def test_foreign_project_event_never_advances_local_completeness(self):
        self.foreign_event(cursor=1);p=self.port();p.register(snapshot_cursor=0);p.cycle()
        global_row=self.admin.execute("SELECT applied_cursor,state FROM coordination.consumer_checkpoints WHERE module_id='graph'").fetchone()
        self.assertEqual(global_row[0],0);self.assertNotEqual(global_row[1],'active')
