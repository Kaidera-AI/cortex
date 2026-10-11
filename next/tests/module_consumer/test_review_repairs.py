"""Frozen native controls for C07-PR69-001/002/003/004."""
import copy
from uuid import UUID

from cortex_core.outbox import Outbox, FeedPage
from cortex_core.modules.protocol import ModuleManifest
from common import ConsumerFixture, FeedOverride, OWNER_A, uid

FIELDS = ('event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale')


def receipt(event):
    env = event.envelope
    return dict(event_id=env['event_id'],aggregate_id=env['aggregate_id'],revision=env['aggregate_revision'],
                tombstone=env['tombstone'],payload_sha256=env['payload_sha256'],generation=1,stale=False)


def bad_receipts(event):
    good = receipt(event)
    cases = [('event-only', {'event_id':good['event_id']})]
    for field in FIELDS:
        missing = dict(good);missing.pop(field);cases.append(('missing-'+field,missing))
        null = dict(good);null[field]=None;cases.append(('null-'+field,null))
    for field,value in (('event_id',7),('aggregate_id',False),('revision',True),('revision','1'),
                        ('revision',1.5),('tombstone',0),('generation',True),('generation','1'),
                        ('stale','false'),('payload_sha256',7),('payload_sha256','0'*64+'\n')):
        wrong = dict(good);wrong[field]=value;cases.append(('type-'+field+'-'+repr(value),wrong))
    extra = dict(good);extra['unknown']=True;cases.append(('extra-property',extra))
    return cases


class ReviewRepairs(ConsumerFixture):
    def page(self):
        return Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).page()

    def feed(self, **changes):
        return FeedOverride(Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))),**changes)

    def no_core_effect(self, module):
        state=self.admin.execute('SELECT applied_cursor FROM coordination.consumer_project_checkpoints WHERE module_id=%s',(module,)).fetchone()
        self.assertEqual(state[0],0)
        for table in ('consumer_aggregate_heads','consumer_event_outcomes'):
            self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.'+table+' WHERE module_id=%s',(module,)).fetchone()[0],0)

    def test_python_complete_typed_receipt_refuses_before_record(self):
        self.seed();event=self.page().events[0]
        for number,(label,bad) in enumerate(bad_receipts(event)):
            with self.subTest(case=label):
                module='python-receipt-'+str(number);p=self.port(module_id=module);p.register(snapshot_cursor=0)
                class NoEffect:
                    def apply(self,value,generation):return copy.deepcopy(bad)
                p.sink=NoEffect()
                with self.assertRaises(self.module().ConsumerError) as caught:p.cycle()
                self.assertEqual(caught.exception.code,'invalid_target_receipt')
                self.no_core_effect(module);self.assertIsNone(self.target())

    def test_sql_complete_typed_receipt_is_null_safe_and_atomic(self):
        self.seed();event=self.page().events[0]
        for number,(label,bad) in enumerate(bad_receipts(event)):
            with self.subTest(case=label):
                module='sql-receipt-'+str(number);p=self.port(module_id=module);p.register(snapshot_cursor=0)
                with self.assertRaises(self.module().ConsumerError):p._record(p._state(),event,'applied',receipt=bad)
                self.no_core_effect(module);self.assertIsNone(self.target())

    def test_conflicting_repair_keeps_successor_fenced(self):
        self.seed(50);self.seed(50,body=b'next',revision=1)
        feed=self.feed(bad_payload_at=1);p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        first=self.page().events[0];feed.bad_payload_at=None;original=self.sink
        class ConflictRoot:
            def apply(self,event,generation):
                if event.cursor==first.cursor:raise ValueError('target_conflict')
                return original.apply(event,generation)
        p.sink=ConflictRoot();p.repair(UUID(first.envelope['event_id']))
        self.assertIsNone(self.target());self.assertEqual(self.checkpoint()[1],0)
        self.assertEqual([r[1] for r in self.outcomes()],['poison','held'])
        self.assertIsNone(self.quarantine()[0][2])

    def test_unrelated_poison_does_not_block_or_change_requested_repair(self):
        self.seed(50);self.seed(51);self.seed(51,body=b'next-b',revision=1);self.seed(50,body=b'next-a',revision=1)
        feed=self.feed(bad_payload_at=1);p=self.port(feed=feed);p.register(snapshot_cursor=0)
        # Both roots are poison at the initial scan, using the original feed fixture.
        base=feed.page
        from cortex_core.outbox import PublishedEvent
        def initial(after=0,limit=100):
            page=base(after=after,limit=limit)
            events=tuple(PublishedEvent(e.cursor,e.envelope,b'bad-b' if e.cursor==2 else e.payload) for e in page.events)
            return FeedPage(events,page.head,page.floor)
        feed.page=initial;p.cycle();feed.page=base
        requested=self.page().events[1]
        try:p.repair(UUID(requested.envelope['event_id']))
        except self.module().ConsumerError as error:
            self.fail('unrelated aggregate blocked requested repair: '+error.code)
        self.assertIsNone(self.target(50));self.assertEqual(self.target(51)[0],2)
        rows=dict(self.outcomes());self.assertEqual(rows[1],'poison');self.assertEqual(rows[4],'held')
        self.assertEqual(self.checkpoint()[1],0)
        unresolved=[row for row in self.quarantine() if row[2] is None]
        self.assertEqual(len(unresolved),1)

    def test_held_event_cannot_be_used_as_quarantine_root(self):
        self.seed(50);self.seed(50,body=b'next',revision=1)
        feed=self.feed(bad_payload_at=1);p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        with self.assertRaises(self.module().ConsumerError) as caught:p.repair(UUID(self.page().events[1].envelope['event_id']))
        self.assertEqual(caught.exception.code,'repair_event_not_poison')
        self.assertIsNone(self.target());self.assertEqual(self.checkpoint()[1],0)

    def test_healthy_completed_prefix_is_compacted_with_durable_head(self):
        p=self.port();p.register(snapshot_cursor=0)
        for revision in range(8):
            self.seed(50,body=('revision-'+str(revision)).encode(),revision=revision);p.cycle()
        self.assertEqual(self.outcomes(),[])
        self.assertEqual(self.checkpoint()[1],8);self.assertEqual(self.target()[0:1],(8,))
        head=self.admin.execute("SELECT revision FROM coordination.consumer_aggregate_heads WHERE module_id='graph'").fetchone()
        self.assertEqual(head[0],8)

    def test_compacted_prefix_replay_is_idempotent_and_cannot_downgrade(self):
        self.seed();p=self.port();p.register(snapshot_cursor=0);event=self.page().events[0];p.cycle()
        self.assertEqual(self.outcomes(),[])
        p._record(p._state(),event,'applied',receipt=receipt(event))
        self.assertEqual(self.outcomes(),[]);self.assertEqual(self.target()[5],1)
        with self.assertRaises(self.module().ConsumerError):p._record(p._state(),event,'poison',error='invalid_digest')
        with self.assertRaises(self.module().ConsumerError):p._record(p._state(),event,'held')
        self.assertEqual(self.quarantine(),[]);self.assertEqual(self.checkpoint()[1],1)

    def test_sparse_poison_held_and_peer_rows_survive_until_gap_closes(self):
        self.seed(50);self.seed(50,body=b'next',revision=1);self.seed(51)
        feed=self.feed(bad_payload_at=1);p=self.port(feed=feed);p.register(snapshot_cursor=0);p.cycle()
        self.assertEqual([r[1] for r in self.outcomes()],['poison','held','applied'])
        first=self.page().events[0];feed.bad_payload_at=None;p.repair(UUID(first.envelope['event_id']))
        self.assertEqual(self.checkpoint()[1],3);self.assertEqual(self.outcomes(),[])
        self.assertEqual(self.target(50)[0],2);self.assertEqual(self.target(51)[5],1)

    def test_manifest_matches_numeric_unique_bounded_kind_schema(self):
        valid=dict(module_id='graph',protocol_version=1,schema_version=1,aggregate_kinds=['memory'],sink='postgres')
        for field,value in (('protocol_version',True),('schema_version',True),('aggregate_kinds',['memory','memory']),('aggregate_kinds',['x'*65])):
            with self.subTest(field=field,value=value):
                bad=dict(valid);bad[field]=value
                with self.assertRaises(ValueError):ModuleManifest.parse(bad)
        for number in (1,1.0):
            good=dict(valid);good['protocol_version']=number;good['schema_version']=number
            self.assertEqual(ModuleManifest.parse(good).aggregate_kinds,('memory',))
        good=dict(valid);good['aggregate_kinds']=['x'*64,'other'];self.assertEqual(len(ModuleManifest.parse(good).aggregate_kinds),2)
