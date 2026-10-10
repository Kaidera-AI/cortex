"""C05 write-only/private-decision boundaries; frozen before the repair."""
import hashlib
import json
import sys
from uuid import UUID
import psycopg
sys.path.insert(0, '/tmp/next/tests')
from core_db_fixture import Fixture, WRITE_A, OWNER_A, uid
from cortex_core.auth import AuthError
from cortex_core.records import Records, RecordError


class PrivateCases(Fixture):
    def records(self):
        return Records(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3)))

    def write_only(self):
        self.admin.execute('UPDATE auth.project_grants SET permissions=%s WHERE tenant_id=%s AND project_id=%s AND principal_id=%s', (['write'], uid(2), uid(3), uid(10)))

    def present(self, signature):
        self.assertIsNotNone(self.admin.execute('SELECT to_regprocedure(%s)', (signature,)).fetchone()[0], 'private decision entry point is absent')

    def refuse(self, error, query, args=()):
        with self.assertRaises(error):
            with self.request.transaction():
                self.request.execute(query, args)

    def test_write_only_delete_replays_without_payload_read(self):
        adapter, record = self.records(), UUID(uid(50))
        initial = adapter.put(record, 'memory', b'exact-private-bytes', 0, 'create')
        self.write_only()
        error = None
        try:
            deleted = adapter.delete(record, 1, 'delete')
        except (RecordError, AuthError) as caught:
            error = caught.code
        self.assertIsNone(error, 'fresh write authority must admit current-revision delete')
        self.assertEqual((deleted.revision, deleted.tombstone, deleted.payload_sha256), (2, True, initial.payload_sha256))
        self.assertEqual(adapter.delete(record, 1, 'delete'), deleted)
        self.assertEqual(self.admin.execute('SELECT count(*) FROM core.payloads WHERE id NOT IN (%s,%s)', (uid(5),uid(5))).fetchone()[0], 1)

    def test_private_decision_does_not_grant_public_read(self):
        self.write_only()
        adapter, record = self.records(), UUID(uid(50))
        adapter.put(record, 'memory', b'exact-private-bytes', 0, 'create')
        with self.assertRaises(AuthError) as caught:
            adapter.get(record)
        self.assertEqual(caught.exception.code, 'forbidden')
        with self.auth(WRITE_A, action='write'):
            for relation in ('core.records','core.record_revisions','core.payloads','coordination.idempotency'):
                self.assertEqual(self.request.execute('SELECT count(*) FROM '+relation).fetchone()[0], 0)

    def test_private_replay_requires_private_bound_write_context(self):
        self.present('coordination.c05_request(text,text)')
        query='SELECT * FROM coordination.c05_request(%s,%s)'
        args=('absent', '0'*64)
        self.refuse(psycopg.errors.InsufficientPrivilege, query, args)
        with self.auth(WRITE_A, action='read'):
            self.refuse(psycopg.errors.InsufficientPrivilege, query, args)
        with self.auth(WRITE_A, action='write'):
            self.request.execute('DISCARD TEMP')
            self.refuse(psycopg.errors.InsufficientPrivilege, query, args)

    def test_replay_is_exact_own_principal_and_request_key(self):
        self.present('coordination.c05_request(text,text)')
        record = UUID(uid(50))
        receipt = self.records().put(record, 'memory', b'own', 0, 'shared-key')
        digest=hashlib.sha256(json.dumps(['put',str(record),'memory',hashlib.sha256(b'own').hexdigest(),0],separators=(',',':')).encode()).hexdigest()
        query='SELECT * FROM coordination.c05_request(%s,%s)'
        with self.auth(OWNER_A, action='write'):
            self.assertEqual(self.request.execute(query, ('shared-key',digest)).fetchall(), [])
        with self.auth(WRITE_A, action='write'):
            self.assertEqual(self.request.execute(query, ('other-key',digest)).fetchall(), [])
            row=self.request.execute(query, ('shared-key',digest)).fetchone()
            self.assertEqual((row[0],row[1],row[2]['record_id'],row[2]['revision']), (digest,'committed',str(record),receipt.revision))
            self.refuse(psycopg.errors.UniqueViolation, query, ('shared-key','0'*64))
            self.request.execute("SELECT set_config('cortex.principal_id',%s,true)", (uid(12),))
            self.assertEqual(self.request.execute(query, ('shared-key',digest)).fetchone(), row)

    def test_direct_private_head_and_payload_enforce_current_cas(self):
        self.present('coordination.c05_update_head(uuid,bigint,bigint,boolean)')
        self.present('coordination.c05_record_payload(uuid,bigint)')
        record = UUID(uid(50))
        self.records().put(record, 'memory', b'own', 0, 'create')
        self.write_only()
        with self.auth(WRITE_A, action='write'):
            for expected,revision in ((0,1),(1,1),(1,3),(2,3)):
                self.refuse(psycopg.errors.UniqueViolation, 'SELECT coordination.c05_update_head(%s,%s,%s,false)', (record,expected,revision))
            self.refuse(psycopg.errors.UniqueViolation, 'SELECT * FROM coordination.c05_record_payload(%s,0)', (record,))
            self.refuse(psycopg.errors.UniqueViolation, 'SELECT * FROM coordination.c05_record_payload(%s,1)', (UUID(uid(99)),))
        self.assertEqual(self.admin.execute('SELECT current_revision,tombstone FROM core.records WHERE id=%s', (record,)).fetchone(), (1,False))
