"""C06 real PostgreSQL capture/publication contract, frozen before implementation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import UUID

import psycopg
from cortex_core.auth import AuthError, authorized
from cortex_core.records import Records, RecordError
from cortex_core.coordination import Jobs, JobError
from cortex_core.identity import Identity, DonorPin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture, OWNER_A, WRITE_A, READ_A, CONTROL_A, API, REQUEST, uid


class OutboxTests(Fixture):
    def port(self, key=OWNER_A, project=3, connection=None):
        try:
            module = importlib.import_module('cortex_core.outbox')
        except ImportError:
            self.fail('C06 outbox port is missing')
        return module.Outbox(connection or self.request, key, UUID(uid(1)), UUID(uid(project)))

    def records(self, key=WRITE_A, project=3, connection=None):
        return Records(connection or self.request, key, UUID(uid(1)), UUID(uid(project)))

    def jobs(self, key=OWNER_A):
        return Jobs(self.request, key, UUID(uid(1)), UUID(uid(3)))

    def identity(self, donor=None):
        return Identity(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)), donor_pin=donor)

    def connection(self):
        c = psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@', API+'@'), autocommit=True)
        c.execute('SET ROLE "'+REQUEST+'"')
        return c

    def count(self, table='coordination.outbox'):
        return self.admin.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def create(self, n=50, body=b'exact\x00bytes\xff', key=None):
        return self.records().put(UUID(uid(n)), 'memory', body, 0, key or 'record-'+str(n))

    def capture(self, kind, external):
        return self.admin.execute('''SELECT o.aggregate_id,o.aggregate_revision,o.event_id,p.body
            FROM core.record_aliases a JOIN coordination.outbox o
            ON(o.tenant_id,o.project_id,o.aggregate_id)=(a.tenant_id,a.project_id,a.record_id)
            JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(o.tenant_id,o.project_id,o.payload_ref)
            WHERE a.source_namespace=%s AND a.external_id=%s ORDER BY o.aggregate_revision''',
            ('cortex.core.'+kind, str(external))).fetchall()

    def age(self):
        self.admin.execute("UPDATE coordination.published_events SET published_at=clock_timestamp()-interval '8 days'")

    def fixture_event(self, c, event, aggregate, old=False):
        # Trusted test/bootstrap writer, not an admitted runtime mutation probe.
        body=b'synthetic late-commit fixture'; digest=hashlib.sha256(body).hexdigest()
        c.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES(%s,%s,%s,%s,%s)',
                  (uid(2),uid(3),uid(aggregate+1000),body,digest))
        c.execute("INSERT INTO core.records VALUES(%s,%s,%s,'memory',1,false)", (uid(2),uid(3),uid(aggregate)))
        c.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,1,%s,false)',
                  (uid(2),uid(3),uid(aggregate),uid(aggregate+1000)))
        c.execute('''INSERT INTO coordination.outbox(event_id,installation_id,tenant_id,project_id,aggregate_id,
            aggregate_kind,aggregate_revision,operation,tombstone,schema_version,payload_ref,payload_sha256,occurred_at)
            VALUES(%s,%s,%s,%s,%s,'memory',1,'upsert',false,1,%s,%s,
                   clock_timestamp()-make_interval(days=>%s))''',
            (uid(event),uid(1),uid(2),uid(3),uid(aggregate),uid(aggregate+1000),digest,8 if old else 0))

    def test_additive_manifest_and_frozen_business_table_boundary(self):
        self.port()
        root=Path(__file__).resolve().parents[2]
        manifest=json.loads((root/'schema/manifest.json').read_bytes())
        entry=next(m for m in manifest['migrations'] if m['id']=='outbox-0002')
        self.assertEqual(entry['sha256'],hashlib.sha256((root/'schema'/entry['file']).read_bytes()).hexdigest())
        self.assertEqual(self.admin.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('core','auth','coordination','retrieval') AND c.relkind='r' AND c.relname<>'schema_migrations'").fetchone()[0],28)

    def test_record_ack_binds_committed_event_and_exact_bytes(self):
        p=self.port(); body=b'exact\x00record\xff'; receipt=self.create(body=body)
        row=self.admin.execute('SELECT event_id,payload_sha256 FROM coordination.outbox WHERE aggregate_id=%s',(uid(50),)).fetchone()
        self.assertEqual((receipt.event_id,receipt.payload_sha256),(row[0],row[1]))
        self.assertEqual(self.count(),1)
        delivered=p.publish()[0]
        self.assertEqual(delivered.payload,body)
        self.assertEqual(delivered.envelope['event_id'],str(receipt.event_id))

    def test_update_and_delete_have_monotonic_revision_and_tombstone(self):
        p=self.port(); self.create(); self.records().put(UUID(uid(50)),'memory',b'new\x00bytes',1,'update')
        deleted=self.records().delete(UUID(uid(50)),2,'delete')
        events=p.publish(); self.assertEqual([e.envelope['aggregate_revision'] for e in events],[1,2,3])
        self.assertEqual([(e.envelope['operation'],e.envelope['tombstone']) for e in events],[('upsert',False),('upsert',False),('delete',True)])
        self.assertEqual((events[-1].payload,deleted.event_id),(b'new\x00bytes',UUID(events[-1].envelope['event_id'])))

    def test_same_request_replays_exact_event_without_new_revision(self):
        self.port(); a=self.create(key='same'); b=self.create(key='same')
        self.assertEqual(a,b); self.assertEqual(self.count(),1)

    def test_changed_request_refuses_without_an_extra_event(self):
        self.port(); self.create(key='same')
        with self.assertRaises(RecordError):self.create(body=b'changed',key='same')
        self.assertEqual(self.count(),1)

    def test_failure_after_actual_history_and_event_rolls_back(self):
        self.port(); module=importlib.import_module('cortex_core.records'); real=module._append_revision
        def fail(*args,**kwargs):
            real(*args,**kwargs)
            self.assertEqual(self.request.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0],1)
            raise RuntimeError('synthetic after actual capture')
        with patch.object(module,'_append_revision',fail),self.assertRaises(RuntimeError):self.create()
        self.assertEqual(self.count(),0)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.records WHERE id=%s',(uid(50),)).fetchone()[0],0)

    def test_direct_bound_record_sql_cannot_commit_without_capture(self):
        self.port(); body=b'direct exact bytes'; digest=hashlib.sha256(body).hexdigest()
        with authorized(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)),'write'):
            self.request.execute('INSERT INTO core.payloads VALUES(%s,%s,%s,%s,%s)',(uid(2),uid(3),uid(150),body,digest))
            self.request.execute("INSERT INTO core.records VALUES(%s,%s,%s,'memory',1,false)",(uid(2),uid(3),uid(50)))
            self.request.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,1,%s,false)',(uid(2),uid(3),uid(50),uid(150)))
        self.assertEqual(self.count(),1)

    def test_raw_bound_job_write_without_parent_capture_refuses_commit(self):
        self.port(); self.jobs().create(UUID(uid(60)),'work',b'intent','create'); before=self.count()
        with self.assertRaises(AuthError) as error:
            with authorized(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)),'write'):
                self.request.execute("UPDATE coordination.jobs SET state='canceled' WHERE id=%s",(uid(60),))
        self.assertEqual(error.exception.code,'core_unavailable')
        self.assertEqual((self.jobs().get(UUID(uid(60))).state,self.count()),('pending',before))

    def test_reserved_alias_and_fact_names_cannot_be_forged_by_writer(self):
        self.port(); self.create()
        with self.assertRaises(AuthError) as error:
            with authorized(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)),'write'):
                self.request.execute("INSERT INTO core.record_aliases VALUES(%s,%s,'cortex.core.job',%s,%s)",(uid(2),uid(3),uid(60),uid(50)))
        self.assertEqual(error.exception.code,'core_unavailable')
        with self.assertRaises(RecordError):self.records().put(UUID(uid(51)),'core.job',b'forged',0,'forged')

    def test_job_create_claim_complete_each_emit_one_stable_aggregate_fact(self):
        self.port(); j=self.jobs(); created=j.create(UUID(uid(60)),'work',b'intent\x00\xff','create')
        claim=self.jobs(WRITE_A).claim(UUID(uid(60)),'claim')
        done=self.jobs(WRITE_A).complete(claim.job_id,claim.attempt_id,claim.fence,b'result\x00\xff','done')
        rows=self.capture('job',uid(60));self.assertEqual([r[1] for r in rows],[1,2,3]);self.assertEqual(len({r[0] for r in rows}),1)
        self.assertEqual([created.event_id,claim.event_id,done.event_id],[r[2] for r in rows])
        self.assertEqual(self.admin.execute('SELECT p.body FROM coordination.job_results r JOIN core.payloads p ON(p.tenant_id,p.project_id,p.id)=(r.tenant_id,r.project_id,r.payload_ref)').fetchone()[0],b'result\x00\xff')

    def test_job_cancel_emits_once_and_replay_does_not_duplicate(self):
        self.port(); j=self.jobs();j.create(UUID(uid(60)),'work',b'intent','create');a=j.cancel(UUID(uid(60)),'cancel');b=j.cancel(UUID(uid(60)),'cancel')
        self.assertEqual(a,b);self.assertEqual([r[1] for r in self.capture('job',uid(60))],[1,2])

    def test_expiry_and_explicit_retry_are_distinct_captured_revisions(self):
        self.port();j=self.jobs();j.create(UUID(uid(60)),'work',b'intent','create');c=self.jobs(WRITE_A).claim(UUID(uid(60)),'claim')
        self.admin.execute("UPDATE coordination.leases SET expires_at=clock_timestamp()-interval '1 second'")
        j.expire(UUID(uid(60)),'expire');j.retry(UUID(uid(60)),'retry')
        self.assertEqual([r[1] for r in self.capture('job',uid(60))],[1,2,3,4]);self.assertEqual(j.get(UUID(uid(60))).state,'pending')

    def test_release_abandon_and_fail_are_all_captured(self):
        self.port()
        for n,operation in enumerate(('release','abandon','fail'),60):
            self.jobs().create(UUID(uid(n)),'work',b'intent','create-'+str(n));worker=self.jobs(WRITE_A);c=worker.claim(UUID(uid(n)),'claim-'+str(n))
            if operation=='fail':worker.fail(c.job_id,c.attempt_id,c.fence,b'exact failure','finish-'+str(n))
            else:getattr(worker,operation)(c.job_id,c.attempt_id,c.fence,'finish-'+str(n))
            self.assertEqual([r[1] for r in self.capture('job',uid(n))],[1,2,3])

    def test_return_accept_and_rework_keep_independent_review_and_events(self):
        self.port()
        for n,operation in enumerate(('accept','rework'),60):
            self.jobs().create(UUID(uid(n)),'handoff',b'intent','create-'+str(n));worker=self.jobs(WRITE_A);c=worker.claim(UUID(uid(n)),'claim-'+str(n))
            worker.return_result(c.job_id,c.attempt_id,c.fence,b'exact return','return-'+str(n))
            with self.assertRaises(AuthError):getattr(worker,operation)(c.job_id,c.attempt_id,c.fence,'bad-'+str(n))
            getattr(self.jobs(),operation)(c.job_id,c.attempt_id,c.fence,'review-'+str(n))
            self.assertEqual([r[1] for r in self.capture('job',uid(n))],[1,2,3,4])

    def test_registration_and_role_change_emit_scoped_identity_facts(self):
        self.port();identity=self.identity();registered=identity.register_agent(UUID(uid(70)),'worker',('worker',),('read','write'),request_key='register')
        identity.set_roles(registered.principal_id,('reviewer',),request_key='roles')
        rows=self.capture('identity',uid(70));self.assertEqual([r[1] for r in rows],[1,2]);self.assertEqual(len({r[0] for r in rows}),1)
        self.assertEqual(registered.event_id,rows[0][2])

    def test_human_registration_does_not_publish_secret_digest_or_attestation(self):
        p=self.port();attestation='synthetic private attestation'
        r=self.identity().register_human(UUID(uid(70)),'human',('reviewer',),('owner',),owner_attestation=attestation,request_key='human')
        events=p.publish();self.assertEqual(len(events),1);body=events[0].payload
        self.assertNotIn(r.secret,body);self.assertNotIn(hashlib.sha256(r.secret).hexdigest().encode(),body);self.assertNotIn(attestation.encode(),body)
        self.assertEqual(json.loads(body)['kind'],'human')

    def test_rotation_and_revocation_use_same_identity_aggregate(self):
        self.port();identity=self.identity();r=identity.register_agent(UUID(uid(70)),'worker',('worker',),('read',),request_key='register')
        rotated=identity.rotate(r.principal_id,r.credential_id,request_key='rotate');identity.revoke(rotated.credential_id,request_key='revoke')
        self.assertEqual([x[1] for x in self.capture('identity',uid(70))],[1,2,3])

    def test_explicit_adoption_captures_every_target_and_replay_is_stable(self):
        self.port();source=json.dumps({'project':'fixture','agents':[
            {'source_id':'10','name':'10','role':'worker'},
            {'source_id':'70','name':'worker','role':'worker'}]},sort_keys=True).encode();sha='a'*40
        donor=DonorPin(sha,source,True)
        manifest={'version':1,'donor':{'released_sha':sha,'source_sha256':hashlib.sha256(source).hexdigest()},'installation_id':uid(1),'project_id':uid(3),
            'legacy_registration_authority':{'cortex-add-agent':'owner_or_admin','lead_registration_requires':'owner_or_admin'},'agents':[
            {'source_project':'fixture','source_id':'10','principal_id':uid(10),'name':'10','kind':'agent','roles':['worker'],'permissions':['read','write']},
            {'source_project':'fixture','source_id':'70','principal_id':uid(70),'name':'worker','kind':'agent','roles':['worker'],'permissions':['read']}]}
        identity=self.identity(donor);preview=identity.dry_run_adoption(manifest);self.assertEqual(self.count(),0)
        identity.apply_adoption(manifest,approved_sha256=preview.manifest_sha256,request_key='adopt');identity.apply_adoption(manifest,approved_sha256=preview.manifest_sha256,request_key='adopt')
        self.assertEqual((len(self.capture('identity',uid(10))),len(self.capture('identity',uid(70))),self.count()),(1,1,2))

    def test_revoked_credential_cannot_replay_event_bearing_record_receipt(self):
        self.port();self.create(key='same');self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(WRITE_A).hexdigest(),))
        with self.assertRaises(AuthError):self.create(key='same')
        self.assertEqual(self.count(),1)

    def test_publication_requires_owner_and_scoped_feed_requires_read(self):
        self.port();self.create()
        for key in (READ_A,WRITE_A,CONTROL_A):
            with self.subTest(key_class='nonowner'),self.assertRaises(AuthError):self.port(key).publish()
        self.port().publish();self.assertEqual(len(self.port(READ_A).page().events),1)
        with self.assertRaises(AuthError):self.port(CONTROL_A).page()

    def test_delivery_cursor_is_separate_from_exact_c01_envelope(self):
        p=self.port();self.create();event=p.publish()[0]
        required={'event_id','installation_id','tenant_id','project_id','aggregate_id','payload_ref','aggregate_kind','aggregate_revision','schema_version','operation','tombstone','payload_sha256','occurred_at'}
        self.assertEqual(set(event.envelope),required);self.assertEqual(event.cursor,1);self.assertEqual(event.envelope['schema_version'],1)
        self.assertEqual(hashlib.sha256(event.payload).hexdigest(),event.envelope['payload_sha256'])

    def test_late_lower_id_commit_is_not_skipped_by_higher_publication(self):
        p=self.port();late=psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True);self.addCleanup(late.close)
        with late.transaction():
            self.fixture_event(late,5000,100)
            with self.admin.transaction():self.fixture_event(self.admin,5001,101)
            first=p.publish();self.assertEqual([(e.cursor,e.envelope['event_id']) for e in first],[(1,uid(5001))])
            self.assertEqual(len(p.page().events),1)
        second=p.publish();self.assertEqual([(e.cursor,e.envelope['event_id']) for e in second],[(2,uid(5000))])
        self.assertEqual(len(p.page().events),2)

    def test_concurrent_publishers_assign_each_event_one_dense_cursor(self):
        self.port()
        for n in range(50,58):self.create(n=n)
        def publish(_):
            with self.connection() as c:return self.port(connection=c).publish(limit=3)
        with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(publish,range(3)))
        flat=[e for group in results for e in group];self.assertEqual(len({e.envelope['event_id'] for e in flat}),8)
        self.assertEqual(sorted(e.cursor for e in flat),list(range(1,9)));self.assertEqual(self.count('coordination.published_events'),8)

    def test_failure_after_actual_journal_insert_rolls_back_cursor_and_publication(self):
        p=self.port();self.create();module=importlib.import_module('cortex_core.outbox');real=module._publish
        def fail(*args,**kwargs):
            result=real(*args,**kwargs);self.assertEqual(self.request.execute('SELECT count(*) FROM coordination.published_events').fetchone()[0],1)
            raise RuntimeError('synthetic after actual publication')
        with patch.object(module,'_publish',fail),self.assertRaises(RuntimeError):p.publish()
        self.assertEqual(self.count('coordination.published_events'),0)
        self.assertEqual(p.page().head,0);self.assertEqual(p.publish()[0].cursor,1)

    def test_committed_lost_publication_response_retries_without_duplicate(self):
        p=self.port();self.create();p.publish();self.assertEqual(p.publish(),())
        self.assertEqual((self.count('coordination.published_events'),p.page().head),(1,1))

    def test_scoped_feed_never_returns_other_project_payloads(self):
        p=self.port();self.create();self.admin.execute('INSERT INTO auth.project_grants(tenant_id,project_id,principal_id,permissions) VALUES(%s,%s,%s,%s)',(uid(2),uid(4),uid(12),['owner']))
        self.records(OWNER_A,4).put(UUID(uid(50)),'memory',b'other-project-secret',0,'other');self.port(project=4).publish();p.publish()
        page=self.port(READ_A).page();self.assertEqual(len(page.events),1);self.assertNotIn(b'other-project-secret',page.events[0].payload)
        self.assertTrue(all(e.envelope['project_id']==uid(3) for e in page.events))

    def test_default_retention_is_seven_days_and_old_cursor_refuses_after_prune(self):
        p=self.port();self.create();p.publish();self.assertEqual(self.admin.execute('SELECT retention_seconds FROM coordination.feed_state').fetchone()[0],604800)
        self.age();p.prune();module=importlib.import_module('cortex_core.outbox')
        with self.assertRaises(module.OutboxError) as error:p.page(after=0)
        self.assertEqual(error.exception.code,'expired');page=p.page(after=1);self.assertEqual((page.floor,page.head,page.events),(1,1,()))

    def test_active_snapshot_floor_blocks_pruning_until_expired(self):
        p=self.port();self.create();p.publish();self.age()
        self.admin.execute("INSERT INTO coordination.snapshot_floors VALUES(%s,%s,'rebuild',0,clock_timestamp()+interval '1 hour')",(uid(1),uid(300)))
        p.prune();self.assertEqual(self.count(),1)
        self.admin.execute("UPDATE coordination.snapshot_floors SET expires_at=clock_timestamp()-interval '1 second'");p.prune();self.assertEqual(self.count(),0)

    def test_unresolved_quarantine_is_not_silently_pruned(self):
        p=self.port();r=self.create();p.publish();self.age()
        self.admin.execute("INSERT INTO coordination.quarantine(tenant_id,project_id,event_id,module_id,error_code) VALUES(%s,%s,%s,'graph','poison')",(uid(2),uid(3),r.event_id))
        p.prune();self.assertEqual(self.count(),1)
        self.admin.execute('UPDATE coordination.quarantine SET resolved_at=clock_timestamp()');p.prune();self.assertEqual(self.count(),0)

    def test_pending_old_event_is_never_pruned_and_expired_consumer_is_marked(self):
        p=self.port()
        with self.admin.transaction():self.fixture_event(self.admin,5000,100,old=True)
        self.admin.execute("INSERT INTO coordination.consumer_checkpoints(installation_id,module_id,last_seen_at,expires_at) VALUES(%s,'graph',clock_timestamp()-interval '8 days',clock_timestamp()-interval '1 day')",(uid(1),))
        p.prune();self.assertEqual(self.count(),1);self.assertEqual(self.admin.execute('SELECT state FROM coordination.consumer_checkpoints').fetchone()[0],'expired')
        self.assertEqual(p.publish()[0].envelope['event_id'],uid(5000))

    def test_invalid_page_bounds_and_closed_core_have_typed_refusals(self):
        p=self.port();module=importlib.import_module('cortex_core.outbox')
        for args in ({'after':True},{'after':-1},{'after':2**63},{'limit':True},{'limit':0},{'limit':1001}):
            with self.subTest(bounds=args),self.assertRaises(module.OutboxError) as error:p.page(**args)
            self.assertEqual(error.exception.code,'invalid_input')
        self.request.close()
        with self.assertRaises(module.OutboxError) as error:p.page()
        self.assertEqual(error.exception.code,'core_unavailable')

    def test_writer_inventory_and_held_admission_contract_are_explicit(self):
        self.port();root=Path(__file__).resolve().parents[2]
        data=json.loads((root/'contracts/outbox-conformance.json').read_bytes())
        self.assertEqual(data['publication'],'locked_committed_journal_dense_cursor')
        self.assertEqual(data['retention_seconds'],604800);self.assertFalse(data['external_http_admission'])
        inventory=json.loads((root/'contracts/core-writer-inventory.json').read_bytes())
        self.assertEqual(inventory['unclassified_writers'],[])
        self.assertTrue({'records','jobs','identity'} <= set(inventory['captured_ports']))
