"""C07 frozen expiry, explicit generation and poison-retention controls."""
from uuid import UUID
from cortex_core.outbox import Outbox
from common import ConsumerFixture, FeedOverride, OWNER_A, uid


class Retention(ConsumerFixture):
    def test_expired_consumer_refuses_checkpoint_and_requires_generation(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0)
        self.admin.execute("UPDATE coordination.published_events SET published_at=clock_timestamp()-interval '8 days'")
        self.assertEqual(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).prune()['removed'],1)
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual(self.checkpoint()[2],'expired')
        with self.assertRaises(self.module().ConsumerError):p.register(snapshot_cursor=1,generation=1)
        p.begin_rebuild(snapshot_cursor=1,generation=2)
        self.assertEqual(self.checkpoint()[3],2)

    def test_snapshot_floor_precedes_replay(self):
        self.seed(50);p=self.port();p.register(snapshot_cursor=0)
        self.admin.execute("UPDATE coordination.published_events SET published_at=clock_timestamp()-interval '8 days'")
        Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).prune()
        self.seed(51);p.begin_rebuild(snapshot_cursor=1,generation=2);p.cycle()
        self.assertIsNone(self.target(50,generation=2))
        self.assertEqual((self.target(51,generation=2)[0],self.checkpoint()[1]),(1,2))

    def test_prune_cannot_drop_unresolved_poison(self):
        self.seed();feed=FeedOverride(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))),bad_payload_at=1)
        p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        self.admin.execute("UPDATE coordination.published_events SET published_at=clock_timestamp()-interval '8 days'")
        result=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).prune()
        self.assertEqual(result['removed'],0)
        self.assertEqual(len(self.quarantine()),1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.published_events').fetchone()[0],1)
