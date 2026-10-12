"""Real scoped rows under a global feed head, with fresh request clients."""
import os
import psycopg
from common import ConsumerFixture,TargetSink
from core_db_fixture import API,REQUEST


class ScopedExhaustion(ConsumerFixture):
    def fresh(self):
        connection=psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',API+'@'),autocommit=True)
        self.addCleanup(connection.close);connection.execute(f'SET ROLE "{REQUEST}"')
        self.sink=TargetSink(self.admin)
        return self.port(connection=connection)

    def continue_cycle(self):
        try:self.fresh().cycle()
        except self.module().ConsumerError as error:self.fail('scoped exhaustion must permit ordinary polling: '+error.code)

    def empty_foreign_head(self):
        self.foreign_event(cursor=1)
        p=self.port();p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual(self.checkpoint()[:2],(1,0))
        self.assertIsNone(self.target());self.assertEqual(self.outcomes(),[])

    def local_with_foreign_tail(self):
        self.seed();self.foreign_event(cursor=2)
        p=self.port();p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual(self.checkpoint()[:2],(2,1))
        self.assertEqual((self.target()[0],self.target()[5]),(1,1))

    def test_empty_project_repeated_cycle_preserves_local_completeness(self):
        self.empty_foreign_head();self.continue_cycle();self.continue_cycle()
        self.assertEqual(self.checkpoint()[:2],(1,0))
        self.assertIsNone(self.target());self.assertEqual(self.outcomes(),[])

    def test_local_event_after_empty_foreign_fence_applies(self):
        self.empty_foreign_head();self.seed();self.continue_cycle()
        self.assertEqual(self.checkpoint()[:2],(2,2))
        self.assertEqual((self.target()[0],self.target()[5]),(1,1))
        self.assertIsNone(self.target(70,project=4));self.assertEqual(self.outcomes(),[])

    def test_completed_local_foreign_tail_repeated_cycle_preserves_effects(self):
        self.local_with_foreign_tail();self.continue_cycle();self.continue_cycle()
        self.assertEqual(self.checkpoint()[:2],(2,1))
        self.assertEqual((self.target()[0],self.target()[5]),(1,1))
        self.assertEqual(self.outcomes(),[])

    def test_local_revision_after_completed_foreign_fence_applies(self):
        self.local_with_foreign_tail();self.seed(body=b'local-second',revision=1);self.continue_cycle()
        self.assertEqual(self.checkpoint()[:2],(3,3))
        self.assertEqual((self.target()[0],self.target()[5]),(2,2))
        self.assertIsNone(self.target(70,project=4));self.assertEqual(self.outcomes(),[])
