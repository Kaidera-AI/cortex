"""C07 frozen aggregate-scoped poison and peer-progress controls."""
from uuid import UUID
from cortex_core.outbox import Outbox
from common import ConsumerFixture, FeedOverride, OWNER_A, uid


class Poison(ConsumerFixture):
    def feed(self, **changes):
        return FeedOverride(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))),**changes)

    def test_poison_blocks_only_its_aggregate(self):
        self.seed(50);self.seed(51);p=self.port(feed=self.feed(bad_payload_at=1));p.register(snapshot_cursor=0)
        p.cycle();self.assertIsNone(self.target(50));self.assertIsNotNone(self.target(51))
        self.assertEqual(len(self.quarantine()),1);self.assertEqual(self.checkpoint()[1],0)

    def test_peer_aggregate_progress_does_not_claim_complete_cursor(self):
        self.seed(50);self.seed(51);p=self.port(feed=self.feed(bad_payload_at=1));p.register(snapshot_cursor=0)
        p.cycle();self.assertEqual(self.checkpoint()[:2],(2,0))
        self.assertEqual([row[1] for row in self.outcomes()],['poison','applied'])

    def test_held_successors_replay_in_order_after_repair(self):
        self.seed(50);self.seed(50,body=b'next',revision=1);self.seed(51)
        feed=self.feed(bad_payload_at=1);p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual([row[1] for row in self.outcomes()],['poison','held','applied'])
        self.assertEqual(self.target(51)[0],1);self.assertIsNone(self.target(50))
        event=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).page().events[0]
        feed.bad_payload_at=None;p.repair(UUID(event.envelope['event_id']))
        self.assertEqual((self.target(50)[0],self.target(50)[5],self.checkpoint()[1]),(2,2,3))
        self.assertIsNotNone(self.quarantine()[0][2])

    def test_transient_failure_does_not_quarantine(self):
        self.seed();self.sink.transient=True;p=self.port();p.register(snapshot_cursor=0)
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual(self.quarantine(),[]);self.assertEqual(self.checkpoint()[1],0)

    def test_untrusted_aggregate_or_scope_refuses_cycle(self):
        self.seed();p=self.port(feed=self.feed(wrong_project_at=1));p.register(snapshot_cursor=0)
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual(self.quarantine(),[]);self.assertEqual(self.checkpoint()[1],0)

    def test_sparse_outcome_capacity_refuses_without_dropping_peer(self):
        self.seed(50);self.seed(51);self.seed(52)
        p=self.port(feed=self.feed(bad_payload_at=1));p.max_pending_outcomes=2
        p.register(snapshot_cursor=0)
        with self.assertRaises(self.module().ConsumerError):p.cycle()
        self.assertEqual([row[1] for row in self.outcomes()],['poison','applied'])
        self.assertIsNotNone(self.target(51));self.assertIsNone(self.target(52))
        self.assertEqual(self.checkpoint()[:2],(2,0))
        with self.assertRaises(self.module().ConsumerError):p.cycle()
