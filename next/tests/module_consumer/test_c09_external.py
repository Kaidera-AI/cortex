"""C09 admitted-Core oracle; S3c rebase authorizes isolated pytest-pg PostgreSQL qualification.

The destination receipt is synthetic: these tests can prove Core durability, never
Qdrant atomicity. Reuse C04/C07 fixtures; no direct execution of an unadmitted SQL
file. The isolation guard runs before the inherited destructive schema reset.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import unittest
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from cortex_core.auth import authorized, AuthError
from cortex_core.embeddings.projection_revision import EmbeddingIdentity
from cortex_core.modules.consumer import ModuleConsumer, ConsumerError
from cortex_core.modules.vector.projection import (
    ProjectionBinding, binding_contract, build_point, point_key, make_receipt, point_id,
)
from cortex_core.outbox import Outbox
from core_db_fixture import Fixture, OWNER_A, READ_A, REQUEST, API, uid
from common import ConsumerFixture

NEXT = Path(__file__).resolve().parents[2]
C09 = 'coordination/008-c09-external-v2.sql'


def admitted_entry():
    entries=json.loads((NEXT/'schema/manifest.json').read_text())['migrations']
    return [e for e in entries if e['file']==C09]


def copied_database():
    """No connection or reset unless the existing URL names this task's copy."""
    url=os.environ.get('TEST_DATABASE_URL','')
    params=conninfo_to_dict(url)
    # Existing U13Fixture creates this DB inside an owned network=none
    # namespace, without published ports. Its parent verifies ownership labels.
    if (url!='postgresql://postgres@127.0.0.1:55432/kaidera-test-upgrade-ci'
            or re.fullmatch(r'local-[0-9a-f]{10}',os.environ.get('CORTEX_C09_COPY_RUN_ID','')) is None):
        raise RuntimeError('C09 tests require the separately authorized task-owned copied database')
    return params


class AdmissionTests(unittest.TestCase):
    def test_c09_is_admitted_once_with_exact_source_bytes(self):
        rows=admitted_entry()
        self.assertEqual(len(rows),1,'C09 remains unadmitted: no durable-chain credit')
        self.assertEqual(rows[0]['sha256'],hashlib.sha256((NEXT/'schema'/C09).read_bytes()).hexdigest())
        entries=json.loads((NEXT/'schema/manifest.json').read_text())['migrations']
        self.assertEqual(len(entries),21)
        self.assertEqual(entries[19]['id'],'runtime-grants-0001')
        self.assertEqual(entries[19]['file'],'auth/007-runtime-grants.sql')
        self.assertEqual(entries[20],dict(id='c09-external-v2-0008',file=C09,
            sha256=hashlib.sha256((NEXT/'schema'/C09).read_bytes()).hexdigest()))


class IsolationTests(unittest.TestCase):
    def test_only_task_owned_copy_url_is_admitted_before_any_connection(self):
        from unittest.mock import patch
        import socket
        good='postgresql://postgres@127.0.0.1:55432/kaidera-test-upgrade-ci'
        bad=['','postgresql://postgres@localhost/cortex',
             good.replace('kaidera-test-upgrade-ci','cortex'),
             good.replace('postgres@','kaidera-test-core-api@'),
             good.replace('127.0.0.1','localhost'),good.replace('55432','5499'),
             good+'?hostaddr=127.0.0.1']
        with patch.dict(os.environ,{'CORTEX_C09_COPY_RUN_ID':'local-0123456789'}), \
             patch.object(psycopg,'connect',side_effect=AssertionError('no DB connect in isolation oracle')), \
             patch.object(socket.socket,'connect',side_effect=AssertionError('no socket connect in isolation oracle')):
            for url in bad:
                with self.subTest(url=url),patch.dict(os.environ,{'TEST_DATABASE_URL':url}):
                    with self.assertRaises(RuntimeError):copied_database()
            with patch.dict(os.environ,{'TEST_DATABASE_URL':good}):
                self.assertEqual(copied_database()['dbname'],'kaidera-test-upgrade-ci')
                for run_id in ('','shared','local-../../live'):
                    with patch.dict(os.environ,{'CORTEX_C09_COPY_RUN_ID':run_id}):
                        with self.assertRaises(RuntimeError):copied_database()


class DurableExternal(Fixture):
    seed=ConsumerFixture.seed
    foreign_event=ConsumerFixture.foreign_event

    def setUp(self):
        self.params=copied_database()  # before Fixture.reset or any connection
        entries=admitted_entry()
        if len(entries)!=1 or entries[0]['sha256']!=hashlib.sha256((NEXT/'schema'/C09).read_bytes()).hexdigest():
            raise RuntimeError('C09 must be admitted by the owner; raw draft execution forbidden')
        super().setUp()
        self.binding=ProjectionBinding(UUID(uid(1)),UUID(uid(2)),UUID(uid(3)),'graph',1,
            UUID(uid(400)),EmbeddingIdentity('synthetic','model','v1',768,'text-v1'))
        self.receipts={}

    def port(self, *, connection=None, after_checkpoint=None):
        parent=self
        class ReceiptSink:
            def apply(self,event,generation):return parent.receipts[event.envelope['event_id']]
        return ModuleConsumer(connection or self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)),
            'graph',ReceiptSink(),manifest=dict(module_id='graph',protocol_version=2,schema_version=1,
                aggregate_kinds=['memory'],sink='qdrant'),external_binding=self.binding,
            after_checkpoint=after_checkpoint)

    def events(self):
        return Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3))).page(limit=1000).events

    def receipt(self,event,observed=None):
        desired=build_point(self.binding,event,[1.,.5]+[0.]*766)
        actual=build_point(self.binding,observed or event,[1.,.5]+[0.]*766)
        value=make_receipt(self.binding,event,desired,actual,7)
        self.receipts[event.envelope['event_id']]=value
        return value

    def checkpoint(self):
        return self.admin.execute('''SELECT scan_cursor,applied_cursor,state,generation
            FROM coordination.consumer_project_checkpoints WHERE module_id='graph'
            AND project_id=%s ORDER BY generation DESC LIMIT 1''',(uid(3),)).fetchone()

    def head(self,aggregate=50,connection=None):
        return (connection or self.admin).execute('''SELECT revision,event_id,payload_sha256,tombstone,effect_sha256,point_id
            FROM coordination.vector_applied_heads WHERE module_id='graph' AND project_id=%s AND aggregate_id=%s''',
            (uid(3),uid(aggregate))).fetchone()

    def fresh(self, *, request=False):
        params=dict(self.params)
        if request:params['user']=API
        connection=psycopg.connect(make_conninfo(**params),autocommit=True)
        self.addCleanup(connection.close)
        if request:connection.execute(f'SET ROLE "{REQUEST}"')
        return connection

    def state(self,ids=()):
        with authorized(self.request,READ_A,UUID(uid(1)),UUID(uid(3)),'read'):
            value=self.request.execute('SELECT coordination.c09_state(%s,%s::uuid[])',('graph',list(ids))).fetchone()[0]
        return value

    def test_sql_and_python_point_ids_match_full_binding(self):
        self.seed();event=self.events()[0];key=point_key(self.binding,UUID(event.envelope['aggregate_id']))
        value=self.admin.execute('SELECT coordination.c09_point_id(%s::jsonb)',(json.dumps(key),)).fetchone()[0]
        self.assertEqual(value,UUID(point_id(key)))
        other=dict(key,collection_generation=str(UUID(uid(401))))
        self.assertNotEqual(value,self.admin.execute('SELECT coordination.c09_point_id(%s::jsonb)',(json.dumps(other),)).fetchone()[0])

    def test_applied_commit_visible_to_fresh_connection_then_replay_is_stable(self):
        self.seed();event=self.events()[0];receipt=self.receipt(event);seen=[]
        observer=self.fresh()
        p=self.port(after_checkpoint=lambda:seen.append(self.head(connection=observer)))
        p.register();p.cycle()
        self.assertEqual(self.checkpoint()[:2],(1,1))
        expected=(1,UUID(event.envelope['event_id']),event.envelope['payload_sha256'],False,
                  receipt['observed']['effect_sha256'],UUID(receipt['point_id']))
        self.assertEqual(seen,[expected]);self.assertEqual(self.head(connection=self.fresh()),expected)
        self.port(connection=self.fresh(request=True))._apply_event({'generation':1},event)
        self.assertEqual(self.head(),expected)
        self.assertEqual(self.checkpoint()[:2],(1,1))

    def test_stale_records_actual_successor_but_does_not_jump_contiguous_cursor(self):
        self.seed();self.seed(body=b'successor',revision=1);first,second=self.events()
        p=self.port();p.register();receipt=self.receipt(first,second)
        p._apply_event({'generation':1},first)
        self.assertEqual(self.head()[:2],(2,UUID(second.envelope['event_id'])))
        self.assertEqual(self.head()[4],receipt['observed']['effect_sha256'])
        self.assertEqual(self.checkpoint()[:2],(1,1))
        # A matching latest vector head cannot substitute for the missing second outcome.
        with self.assertRaises(AuthError):self.state((UUID(uid(50)),))
        self.receipt(second);p._apply_event({'generation':1},second)
        self.assertEqual(self.checkpoint()[:2],(2,2))
        self.assertTrue(self.state((UUID(uid(50)),))['targets'][0]['indexed'])

    def test_vector_head_failure_rolls_back_existing_c07_outcome_and_prefix(self):
        self.seed();event=self.events()[0];self.receipt(event);p=self.port();p.register()
        self.admin.execute("""CREATE FUNCTION public.c09_test_refuse() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic_vector_head_failure'; END $$""")
        self.addCleanup(self.admin.execute,'DROP FUNCTION IF EXISTS public.c09_test_refuse() CASCADE')
        self.admin.execute('''CREATE TRIGGER c09_test_refuse BEFORE INSERT OR UPDATE
            ON coordination.vector_applied_heads FOR EACH ROW EXECUTE FUNCTION public.c09_test_refuse()''')
        with self.assertRaises(ConsumerError) as caught:p._apply_event({'generation':1},event)
        self.assertEqual(caught.exception.code,'core_unavailable')
        self.assertEqual(self.checkpoint()[:2],(0,0));self.assertIsNone(self.head())
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.consumer_aggregate_heads WHERE module_id='graph'").fetchone()[0],0)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.consumer_event_outcomes WHERE module_id='graph'").fetchone()[0],0)
        self.admin.execute('DROP TRIGGER c09_test_refuse ON coordination.vector_applied_heads')
        p._apply_event({'generation':1},event)
        self.assertEqual(self.checkpoint()[:2],(1,1))
        self.assertIsNotNone(self.head(connection=self.fresh()))

    def test_poison_holds_prefix_across_other_aggregate_and_fresh_client(self):
        self.seed(50);self.seed(51);first,second=self.events();self.receipt(first);self.receipt(second)
        p=self.port();p.register();p._record({'generation':1},first,'poison',error='invalid_digest')
        p._apply_event({'generation':1},second)
        self.assertEqual(self.checkpoint()[:2],(2,0));self.assertIsNone(self.head(50));self.assertIsNotNone(self.head(51))
        fresh=self.port(connection=self.fresh(request=True));fresh.repair(UUID(first.envelope['event_id']))
        self.assertEqual(self.checkpoint()[:2],(2,2))
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.consumer_event_outcomes WHERE module_id='graph'").fetchone()[0],0)

    def test_direct_v1_and_foreign_key_receipts_cannot_credit_bound_generation(self):
        self.seed();event=self.events()[0];receipt=self.receipt(event);p=self.port();p.register()
        v1={k:receipt[k] for k in ('event_id','aggregate_id','revision','tombstone','payload_sha256','generation','stale')}
        variants=[v1,dict(receipt,proof='acknowledged'),dict(receipt,operation_id=True),
                  dict(receipt,key=dict(receipt['key'],project_id=uid(4))),
                  dict(receipt,observed=dict(receipt['observed'],event_id=uid(999)))]
        for value in variants:
            with self.subTest(value=list(value)):
                with self.assertRaises(ConsumerError):p._record({'generation':1},event,'applied',receipt=value)
                self.assertEqual(self.checkpoint()[:2],(0,0));self.assertIsNone(self.head())
        with authorized(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)),'control'):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.request.transaction():
                    self.request.execute('SELECT coordination.c07_record_v1(%s,%s,%s,%s,%s,%s,%s::jsonb)',
                        ('graph',1,event.cursor,event.envelope['event_id'],'applied',None,json.dumps(v1)))

    def test_private_tables_force_rls_and_request_role_cannot_write_or_read_them(self):
        self.port().register()
        rows=self.admin.execute("""SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,
            pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='coordination' AND c.relname IN ('vector_bindings','vector_applied_heads') ORDER BY c.relname""").fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r[1:]==(True,True,'kaidera-runtime-core-verifier') for r in rows))
        for table in ('vector_bindings','vector_applied_heads'):
            for verb in ('SELECT * FROM','DELETE FROM'):
                with self.subTest(table=table,verb=verb):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        self.request.execute(f'{verb} coordination.{table}')
        self.assertFalse(self.admin.execute("SELECT has_function_privilege(%s,'coordination.c09_point_id(jsonb)','EXECUTE')",(REQUEST,)).fetchone()[0])

    def test_binding_is_immutable_and_snapshot_rebuild_cannot_claim_materialization(self):
        p=self.port();p.register();bad=dict(binding_contract(self.binding),collection_generation=uid(401))
        with self.assertRaises(ConsumerError):p._call('c09_bind(%s,%s,%s)',('graph',1,json.dumps(bad)))
        self.seed();event=self.events()[0];self.receipt(event);p.cycle()
        with self.assertRaises(ConsumerError):p.begin_rebuild(snapshot_cursor=1,generation=2)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.vector_bindings WHERE generation=2").fetchone()[0],0)
        with self.assertRaises(AuthError):self.state()

    def test_retained_floor_expiry_refuses_old_core_proof(self):
        self.seed();event=self.events()[0];self.receipt(event);p=self.port();p.register();p.cycle()
        self.admin.execute('''UPDATE coordination.feed_state SET project_retained_floors=%s::jsonb
            WHERE installation_id=%s''',(json.dumps({uid(2)+':'+uid(3):2}),uid(1)))
        with self.assertRaises(AuthError):self.state((UUID(uid(50)),))

    def test_concurrent_stale_and_successor_share_the_c07_checkpoint_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import time
        self.seed();self.seed(body=b'successor',revision=1);first,second=self.events()
        self.receipt(first,second);self.receipt(second);self.port().register()
        connections=[self.fresh(request=True),self.fresh(request=True)]
        for connection in connections:connection.execute("SET statement_timeout='5s'")
        ready=threading.Barrier(3)
        def record(connection,event):
            ready.wait(timeout=5)
            self.port(connection=connection)._apply_event({'generation':1},event)
        pids=[connection.info.backend_pid for connection in connections]
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.admin.transaction():
                self.admin.execute("""SELECT 1 FROM coordination.consumer_project_checkpoints
                    WHERE module_id='graph' AND project_id=%s FOR UPDATE""",(uid(3),))
                futures=[pool.submit(record,connection,event)
                         for connection,event in zip(connections,(first,second))]
                ready.wait(timeout=5)
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    self.admin.execute('SELECT pg_stat_clear_snapshot()')
                    waiting=self.admin.execute("""SELECT count(*) FROM pg_stat_activity
                        WHERE pid=ANY(%s) AND wait_event_type='Lock'""",(pids,)).fetchone()[0]
                    if waiting==2:break
                    time.sleep(.02)
                self.assertEqual(waiting,2,'both real connections must wait on the existing checkpoint row')
                self.assertEqual(self.checkpoint()[:2],(0,0));self.assertIsNone(self.head())
            for future in futures:future.result(timeout=5)
        self.assertEqual(self.checkpoint()[:2],(2,2))
        self.assertEqual(self.head()[:2],(2,UUID(second.envelope['event_id'])))
        self.assertEqual(self.admin.execute("SELECT count(*) FROM coordination.consumer_event_outcomes WHERE module_id='graph'").fetchone()[0],0)
        self.assertTrue(self.state((UUID(uid(50)),))['targets'][0]['indexed'])

    def test_foreign_project_published_head_does_not_block_local_core_proof(self):
        self.seed();event=self.events()[0];self.receipt(event);p=self.port();p.register();p.cycle()
        self.foreign_event(cursor=2)
        self.assertEqual(self.checkpoint()[:2],(1,1))
        self.assertTrue(self.state((UUID(uid(50)),))['targets'][0]['indexed'])
