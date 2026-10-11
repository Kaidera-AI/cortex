"""Durable resume through real Core/target state, fresh client and PG connection."""
import os
from uuid import UUID
import psycopg
from cortex_core.outbox import Outbox,FeedPage,PublishedEvent
from common import ConsumerFixture,FeedOverride,TargetSink,OWNER_A,uid
from core_db_fixture import API,REQUEST


class DurableContinuation(ConsumerFixture):
    def fresh(self):
        connection=psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',API+'@'),autocommit=True)
        self.addCleanup(connection.close);connection.execute(f'SET ROLE "{REQUEST}"')
        self.sink=TargetSink(self.admin)
        return self.port(connection=connection)

    def setup_held(self):
        self.seed(50);self.seed(50,body=b'held',revision=1)
        feed=FeedOverride(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))),bad_payload_at=1)
        p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        first=feed.port.page().events[0];feed.bad_payload_at=None
        return p,first

    def resume(self,p):
        try:p.cycle(limit=1000)
        except self.module().ConsumerError as error:self.fail('durable observed continuation must resume: '+error.code)

    def test_fresh_client_resumes_after_root_checkpoint_interruption(self):
        p,first=self.setup_held()
        def stop():raise TimeoutError('after_root_commit')
        p.after_checkpoint=stop
        with self.assertRaises(self.module().ConsumerError) as caught:p.repair(UUID(first.envelope['event_id']))
        self.assertEqual(caught.exception.code,'after_checkpoint')
        self.assertEqual(self.checkpoint()[:2],(2,1));self.assertEqual(self.outcomes(),[(2,'held')])
        self.assertIsNotNone(self.quarantine()[0][2]);self.assertEqual(self.target(50)[0],1)
        new=self.fresh();self.sink.transient=True
        # Even another temporary retry failure cannot erase durable held work.
        with self.assertRaises(self.module().ConsumerError) as retry_error:new.cycle()
        self.assertEqual(retry_error.exception.code,'target_unavailable')
        self.assertEqual(self.outcomes(),[(2,'held')])
        again=self.fresh();self.resume(again)
        self.assertEqual(self.checkpoint()[:2],(2,2));self.assertEqual(self.outcomes(),[])
        self.assertEqual((self.target(50)[0],self.target(50)[5]),(2,2))
        with self.assertRaises(self.module().ConsumerError):again.repair(UUID(first.envelope['event_id']))

    def test_retry_after_held_target_commit_has_exactly_once_effect(self):
        p,first=self.setup_held();original=self.sink
        class CommitThenFail:
            def apply(self,event,generation):
                if event.cursor==2:original.fail_after_once=True
                return original.apply(event,generation)
        p.sink=CommitThenFail()
        with self.assertRaises(self.module().ConsumerError) as caught:p.repair(UUID(first.envelope['event_id']))
        self.assertEqual(caught.exception.code,'target_unavailable')
        self.assertEqual(self.checkpoint()[:2],(2,1));self.assertEqual(self.outcomes(),[(2,'held')])
        self.assertEqual((self.target(50)[0],self.target(50)[5]),(2,2))
        new=self.fresh();self.resume(new)
        self.assertEqual(self.checkpoint()[:2],(2,2));self.assertEqual(self.outcomes(),[])
        self.assertEqual((self.target(50)[0],self.target(50)[5]),(2,2))

    def test_independent_poison_and_full_capacity_survive_multi_page_resume(self):
        self.seed(50)
        for cursor in range(2,1001):self.seed(10000+cursor)
        self.seed(51)
        for cursor in range(1002,2001):self.seed(10000+cursor)
        self.seed(51,body=b'held-b',revision=1)
        base=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        def initial(after=0,limit=100):
            page=base.page(after=after,limit=limit)
            return FeedPage(tuple(PublishedEvent(e.cursor,e.envelope,b'bad' if e.cursor in (1,1001) else e.payload)
                                  for e in page.events),page.head,page.floor)
        p=self.port(feed=type('Feed',(),{'page':staticmethod(initial)})());p.register(snapshot_cursor=0)
        for _ in range(3):p.cycle(limit=1000)
        root=base.page(after=1000,limit=1000).events[0];p.feed=base
        def stop():raise TimeoutError('after_late_root_commit')
        p.after_checkpoint=stop
        with self.assertRaises(self.module().ConsumerError):p.repair(UUID(root.envelope['event_id']))
        self.assertEqual(self.checkpoint()[:2],(2001,0));self.assertEqual(dict(self.outcomes())[2001],'held')
        new=self.fresh();new.max_pending_outcomes=2001;self.resume(new)
        self.assertEqual((self.target(51)[0],self.target(51)[5]),(2,2))
        self.assertIsNone(self.target(50));self.assertEqual(dict(self.outcomes())[1],'poison')
        self.assertEqual(self.target(10002)[5],1)
        self.assertEqual(self.checkpoint()[:2],(2001,0))
        self.assertEqual(len([q for q in self.quarantine() if q[2] is None]),1)
        with self.assertRaises(self.module().ConsumerError):new.repair(UUID(root.envelope['event_id']))
