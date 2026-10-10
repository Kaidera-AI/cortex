"""C07 copied-PG fixture: real Core feed and a separately committed target sink."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
from uuid import UUID

from cortex_core.records import Records
from cortex_core.outbox import Outbox

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, uid


class TargetSink:
    def __init__(self, connection, module_id='graph'):
        self.connection = connection
        self.module_id = module_id
        self.fail_before = False
        self.fail_after_once = False
        self.transient = False

    def apply(self, event, generation):
        if self.fail_before or self.transient:
            raise TimeoutError('synthetic_target_unavailable')
        env = event.envelope
        aggregate = env['aggregate_id']
        existing = self.connection.execute('''SELECT revision,event_id,payload_sha256,tombstone,apply_count
            FROM public.c07_target WHERE module_id=%s AND project_id=%s AND aggregate_id=%s AND generation=%s''',
            (self.module_id, env['project_id'], aggregate, generation)).fetchone()
        if existing:
            if existing[0] > env['aggregate_revision']:
                return self.receipt(event,generation,stale=True)
            if existing[0] == env['aggregate_revision']:
                if existing[1] != UUID(env['event_id']) or existing[2] != env['payload_sha256']:
                    raise ValueError('target_conflict')
                return self.receipt(event,generation)
        with self.connection.transaction():
            self.connection.execute('''INSERT INTO public.c07_target
                (module_id,project_id,aggregate_id,generation,revision,event_id,payload_sha256,tombstone,payload,apply_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON CONFLICT(module_id,project_id,aggregate_id,generation) DO UPDATE SET
                revision=EXCLUDED.revision,event_id=EXCLUDED.event_id,payload_sha256=EXCLUDED.payload_sha256,
                tombstone=EXCLUDED.tombstone,payload=EXCLUDED.payload,apply_count=public.c07_target.apply_count+1''',
                (self.module_id,env['project_id'],aggregate,generation,env['aggregate_revision'],env['event_id'],
                 env['payload_sha256'],env['tombstone'],event.payload))
        if self.fail_after_once:
            self.fail_after_once = False
            raise TimeoutError('synthetic_crash_after_durable_apply')
        return self.receipt(event,generation)

    def receipt(self,event,generation,stale=False):
        e=event.envelope
        return dict(event_id=e['event_id'],aggregate_id=e['aggregate_id'],revision=e['aggregate_revision'],
                    tombstone=e['tombstone'],payload_sha256=e['payload_sha256'],generation=generation,stale=stale)


class ConsumerFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.admin.execute('DROP TABLE IF EXISTS public.c07_target')
        self.admin.execute('''CREATE TABLE public.c07_target(
            module_id text NOT NULL,project_id uuid NOT NULL,aggregate_id uuid NOT NULL,generation bigint NOT NULL,
            revision bigint NOT NULL,event_id uuid NOT NULL,payload_sha256 text NOT NULL,tombstone boolean NOT NULL,
            payload bytea NOT NULL,apply_count integer NOT NULL,
            PRIMARY KEY(module_id,project_id,aggregate_id,generation))''')
        self.addCleanup(self.admin.execute,'DROP TABLE IF EXISTS public.c07_target')
        self.sink = TargetSink(self.admin)

    def module(self):
        try:
            return importlib.import_module('cortex_core.modules.consumer')
        except ImportError:
            self.fail('C07 module consumer port is missing')

    def port(self,project=3,module_id='graph',feed=None,after_checkpoint=None,connection=None):
        module=self.module()
        return module.ModuleConsumer(connection or self.request,OWNER_A,UUID(uid(1)),UUID(uid(project)),
            module_id,self.sink,manifest=dict(module_id=module_id,protocol_version=1,schema_version=1,
                aggregate_kinds=['memory'],sink='postgres'),feed=feed,after_checkpoint=after_checkpoint)

    def seed(self,aggregate=50,body=b'first',revision=0,project=3,key=None):
        records=Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(project)))
        result=records.put(UUID(uid(aggregate)),'memory',body,revision,key or f'c07-{aggregate}-{revision}')
        Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(project))).publish()
        return result

    def target(self,aggregate=50,project=3,generation=1):
        return self.admin.execute('''SELECT revision,event_id,payload_sha256,tombstone,payload,apply_count
            FROM public.c07_target WHERE module_id='graph' AND project_id=%s AND aggregate_id=%s AND generation=%s''',
            (uid(project),uid(aggregate),generation)).fetchone()

    def checkpoint(self,project=3):
        return self.admin.execute('''SELECT scan_cursor,applied_cursor,state,generation FROM coordination.consumer_project_checkpoints
            WHERE installation_id=%s AND module_id='graph' AND tenant_id=%s AND project_id=%s ORDER BY generation DESC LIMIT 1''',
            (uid(1),uid(2),uid(project))).fetchone()

    def quarantine(self):
        return self.admin.execute("SELECT event_id,error_code,resolved_at FROM coordination.quarantine WHERE module_id='graph' ORDER BY event_id").fetchall()

    def outcomes(self):
        return self.admin.execute("SELECT cursor,outcome FROM coordination.consumer_event_outcomes WHERE module_id='graph' ORDER BY cursor").fetchall()

    def foreign_event(self,cursor=1,project=4,aggregate=70,event=90):
        body=b'foreign';digest=hashlib.sha256(body).hexdigest()
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES(%s,%s,%s,%s,%s)',
                           (uid(2),uid(project),uid(aggregate+1000),body,digest))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES(%s,%s,%s,'memory',1,false)",(uid(2),uid(project),uid(aggregate)))
            self.admin.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,1,%s,false)',
                               (uid(2),uid(project),uid(aggregate),uid(aggregate+1000)))
            self.admin.execute('''INSERT INTO coordination.outbox(event_id,installation_id,tenant_id,project_id,
                aggregate_id,aggregate_kind,aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256)
                VALUES(%s,%s,%s,%s,%s,'memory',1,'upsert',false,1,%s,%s)''',
                (uid(event),uid(1),uid(2),uid(project),uid(aggregate),uid(aggregate+1000),digest))
        self.admin.execute('''INSERT INTO coordination.published_events(installation_id,cursor,tenant_id,project_id,event_id)
            VALUES(%s,%s,%s,%s,%s)''',(uid(1),cursor,uid(2),uid(project),uid(event)))
        self.admin.execute('''INSERT INTO coordination.feed_state(installation_id,last_published_cursor)
            VALUES(%s,%s) ON CONFLICT(installation_id) DO UPDATE SET
            last_published_cursor=GREATEST(coordination.feed_state.last_published_cursor,EXCLUDED.last_published_cursor)''',
            (uid(1),cursor))

class FeedOverride:
    def __init__(self, port, *, bad_payload_at=None, wrong_project_at=None):
        self.port=port
        self.bad_payload_at=bad_payload_at
        self.wrong_project_at=wrong_project_at

    def page(self, after=0, limit=100):
        from cortex_core.outbox import FeedPage, PublishedEvent
        page=self.port.page(after=after,limit=limit)
        events=[]
        for event in page.events:
            envelope=dict(event.envelope)
            payload=event.payload
            if event.cursor==self.bad_payload_at:payload=b'changed-but-known-aggregate'
            if event.cursor==self.wrong_project_at:envelope['project_id']=uid(4)
            events.append(PublishedEvent(event.cursor,envelope,payload))
        return FeedPage(tuple(events),page.head,page.floor)
