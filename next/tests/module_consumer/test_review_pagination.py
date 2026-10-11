"""Original-fixture native repair pagination contracts, RED before the loop."""
from uuid import UUID
from cortex_core.outbox import Outbox
from common import ConsumerFixture, FeedOverride, OWNER_A, uid


class Pagination(ConsumerFixture):
    def page(self):
        return Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).page(limit=1000)

    def repair_without_abort(self, port, event):
        try:
            port.repair(UUID(event.envelope['event_id']))
        except self.module().ConsumerError as error:
            self.fail('observed paginated repair must recover without abort: '+error.code)

    def test_1001_observed_events_replay_held_successor(self):
        self.seed(50)
        for cursor in range(2,1001):self.seed(10000+cursor)
        self.seed(50,body=b'held',revision=1)
        feed=FeedOverride(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))),bad_payload_at=1)
        p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle(limit=1000);p.cycle(limit=1000)
        self.assertEqual(self.checkpoint()[:2],(1001,0))
        first=self.page().events[0];feed.bad_payload_at=None
        self.repair_without_abort(p,first)
        self.assertEqual(self.target(50)[0],2)
        self.assertEqual(self.checkpoint()[:2],(1001,1001))
        self.assertEqual(self.outcomes(),[])
        p.cycle();self.assertEqual(self.target(50)[5],2)

    def test_late_root_three_pages_conflict_isolation_and_observed_fence(self):
        self.seed(50)
        for cursor in range(2,1001):self.seed(10000+cursor)
        self.seed(51)
        for cursor in range(1002,2001):self.seed(10000+cursor)
        self.seed(51,body=b'held-b',revision=1)
        base=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        feed=FeedOverride(base,bad_payload_at=1);original_page=feed.page
        from cortex_core.outbox import PublishedEvent,FeedPage
        def poisoned(after=0,limit=100):
            page=original_page(after=after,limit=limit)
            return FeedPage(tuple(PublishedEvent(e.cursor,e.envelope,b'bad-b' if e.cursor==1001 else e.payload)
                                  for e in page.events),page.head,page.floor)
        feed.page=poisoned;p=self.port(feed=feed);p.register(snapshot_cursor=0)
        for _ in range(3):p.cycle(limit=1000)
        self.assertEqual(self.checkpoint()[:2],(2001,0))
        roots=(base.page(limit=1000).events[0],base.page(after=1000,limit=1000).events[0])
        feed.page=base.page;original_sink=self.sink;calls=[]
        class Conflict:
            def apply(self,event,generation):
                calls.append(event.cursor)
                if event.cursor==1001:raise ValueError('target_conflict')
                return original_sink.apply(event,generation)
        p.sink=Conflict();self.repair_without_abort(p,roots[1])
        self.assertEqual(calls,[1001]);self.assertIsNone(self.target(50));self.assertIsNone(self.target(51))
        self.assertEqual(dict(self.outcomes())[2001],'held');self.assertEqual(self.checkpoint()[1],0)
        self.seed(51,body=b'unobserved-b',revision=2)
        p.sink=original_sink;self.repair_without_abort(p,roots[1])
        self.assertEqual(self.target(51)[0],2);self.assertIsNone(self.target(50))
        self.assertEqual(self.checkpoint()[:2],(2001,0));self.assertEqual(dict(self.outcomes())[1],'poison')
        self.assertEqual(len([q for q in self.quarantine() if q[2] is None]),1)
        p.cycle();self.assertEqual(self.target(51)[0],3)
        self.repair_without_abort(p,roots[0])
        self.assertEqual(self.target(50)[0],1)
        self.assertEqual(self.checkpoint()[:2],(2002,2002));self.assertEqual(self.outcomes(),[])
