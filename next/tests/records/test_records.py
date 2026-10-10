"""C05 real-PG record adapter contract, frozen before implementation."""
import hashlib
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import UUID
import psycopg
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core_db_fixture import Fixture,READ_A,WRITE_A,OWNER_A,READ_B,API,REQUEST,uid
from cortex_core.auth import AuthError
try:
    from cortex_core import records as port
except ImportError:
    port=None

class RecordAdapters(Fixture):
    def adapter(self,key=WRITE_A,installation=1,connection=None):
        self.assertIsNotNone(port,'C05 record adapter is missing')
        return port.Records(connection or self.request,key,UUID(uid(installation)),UUID(uid(3)))
    def create(self,key='create',record=50,body=b'exact module bytes\x00\xff'):
        return self.adapter().put(UUID(uid(record)),'memory',body,0,key)
    def error(self,code,call):
        self.assertIsNotNone(port,'C05 record adapter is missing')
        with self.assertRaises(port.RecordError) as caught:call()
        self.assertEqual(caught.exception.code,code)
    def other_connection(self):
        c=psycopg.connect(os.environ['TEST_DATABASE_URL'].replace('postgres@',API+'@'),autocommit=True)
        c.execute(f'SET ROLE "{REQUEST}"');return c
    def test_create_read_preserves_exact_bytes_and_digest(self):
        body=b'exact module bytes\x00\xff';a=self.create(body=body)
        self.assertEqual((a.record_id,a.revision,a.tombstone,a.payload_sha256),(UUID(uid(50)),1,False,hashlib.sha256(body).hexdigest()))
        r=self.adapter(READ_A).get(UUID(uid(50)))
        self.assertEqual((r.body,r.kind,r.revision,r.tombstone),(body,'memory',1,False))
        self.assertEqual(self.admin.execute('SELECT body FROM core.payloads WHERE id=%s',(str(r.payload_id),)).fetchone()[0],body)
    def test_update_compare_and_swap_is_atomic(self):
        self.create();a=self.adapter().put(UUID(uid(50)),'memory',b'new exact bytes',1,'update')
        self.assertEqual(a.revision,2);self.assertEqual(self.adapter().get(UUID(uid(50))).body,b'new exact bytes')
        self.error('conflict',lambda:self.adapter().put(UUID(uid(50)),'memory',b'stale',1,'stale'))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(uid(50),)).fetchone()[0],2)
    def test_delete_is_revisioned_tombstone_and_does_not_erase_history(self):
        self.create();a=self.adapter().delete(UUID(uid(50)),1,'delete');self.assertEqual((a.revision,a.tombstone),(2,True))
        self.assertIsNone(self.adapter(READ_A).get(UUID(uid(50))))
        self.assertTrue(self.adapter(READ_A).get(UUID(uid(50)),include_tombstone=True).tombstone)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(uid(50),)).fetchone()[0],2)
        self.error('conflict',lambda:self.adapter().put(UUID(uid(50)),'memory',b'resurrect',1,'resurrect'))
    def test_same_request_after_committed_lost_response_replays_one_result(self):
        first=self.create(key='stable-request')
        # Actual write has committed; fail before delivery, then retry the same request.
        try:raise ConnectionError('synthetic lost response after actual commit')
        except ConnectionError:pass
        second=self.create(key='stable-request');self.assertEqual(first,second)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(uid(50),)).fetchone()[0],1)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.idempotency WHERE request_key=%s',('stable-request',)).fetchone()[0],1)
    def test_changed_body_kind_record_or_operation_under_key_refuses(self):
        self.create(key='bound')
        for call in (lambda:self.create(key='bound',body=b'changed'),lambda:self.create(key='bound',record=51),
                     lambda:self.adapter().put(UUID(uid(50)),'graph',b'exact module bytes\x00\xff',0,'bound'),
                     lambda:self.adapter().delete(UUID(uid(50)),1,'bound')):
            with self.subTest():self.error('conflict',call)
    def test_principal_scoped_same_key_cannot_return_another_receipt(self):
        first=self.create(key='same-key');second=self.adapter(OWNER_A).put(UUID(uid(51)),'memory',b'owner',0,'same-key')
        self.assertNotEqual(first.record_id,second.record_id)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.idempotency WHERE request_key=%s',('same-key',)).fetchone()[0],2)
    def test_read_key_cannot_mutate_and_cross_tenant_id_is_hidden(self):
        adapter=self.adapter(READ_A)
        with self.assertRaises(AuthError):adapter.put(UUID(uid(50)),'memory',b'bad',0,'bad')
        self.create();self.assertIsNone(self.adapter(READ_B,20).get(UUID(uid(50))))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.records WHERE id=%s AND tenant_id=%s',(uid(50),uid(21))).fetchone()[0],0)
    def test_revoked_credential_cannot_replay_an_old_receipt(self):
        self.create(key='revoked')
        self.admin.execute('UPDATE auth.credentials SET revoked_at=clock_timestamp() WHERE key_digest=%s',(hashlib.sha256(WRITE_A).hexdigest(),))
        with self.assertRaises(AuthError):self.create(key='revoked')
    def test_kind_cannot_change_after_creation(self):
        self.create();self.error('conflict',lambda:self.adapter().put(UUID(uid(50)),'graph',b'changed',1,'change-kind'))
        self.assertEqual(self.adapter().get(UUID(uid(50))).kind,'memory')
    def test_invalid_inputs_refuse_without_partial_canonical_rows(self):
        adapter=self.adapter();before=self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0]
        for args in ((UUID(uid(50)),'memory\n',b'data',0,'key'),(UUID(uid(50)),'memory','text',0,'key'),
                     (UUID(uid(50)),'memory',b'data',True,'key'),(UUID(uid(50)),'memory',b'data',0,''),
                     ('forged','memory',b'data',0,'key')):
            with self.subTest():self.error('invalid_input',lambda args=args:adapter.put(*args))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads').fetchone()[0],before)
    def test_failure_after_history_append_rolls_back_every_partial_row(self):
        adapter=self.adapter();counts={t:self.admin.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('core.payloads','core.records','core.record_revisions','coordination.idempotency')}
        real=port._append_revision
        def fail(*args,**kwargs):real(*args,**kwargs);raise RuntimeError('synthetic after-history failure')
        with patch.object(port,'_append_revision',side_effect=fail),self.assertRaises(RuntimeError):adapter.put(UUID(uid(50)),'memory',b'partial',0,'partial')
        for t,count in counts.items():self.assertEqual(self.admin.execute('SELECT count(*) FROM '+t).fetchone()[0],count,t)
    def test_concurrent_same_key_has_one_logical_result(self):
        self.adapter()
        def write(_):
            with self.other_connection() as c:return self.adapter(connection=c).put(UUID(uid(50)),'memory',b'race',0,'race')
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(uid(50),)).fetchone()[0],1)
    def test_concurrent_record_revision_has_one_winner(self):
        self.create()
        def write(i):
            with self.other_connection() as c:
                try:return self.adapter(connection=c).put(UUID(uid(50)),'memory',str(i).encode(),1,'race-'+str(i)).revision
                except port.RecordError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(write,range(2)))
        self.assertEqual(sorted(results,key=str),[2,'conflict'])
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.record_revisions WHERE record_id=%s',(uid(50),)).fetchone()[0],2)
    def test_closed_core_connection_fails_typed_without_result(self):
        adapter=self.adapter();self.request.close()
        with self.assertRaises(AuthError) as caught:adapter.put(UUID(uid(50)),'memory',b'closed',0,'closed')
        self.assertEqual(caught.exception.code,'core_unavailable')
