"""Actual C11 canonical reader, instrumented C04/DB boundaries; no PostgreSQL."""
from contextlib import contextmanager
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import UUID

from cortex_core.api import c11b
from cortex_core.auth import AuthError
from cortex_core.embeddings.pg_search import CapabilityUnavailable

ID=UUID('00000000-0000-0000-0000-000000000001')


class CurrentCoreReader(unittest.TestCase):
    def probe(self,*,foreign=False,expired=False):
        calls=[];clock=[100.0]
        identity=NS(tenant_id=ID,project_id=ID,principal_id=ID,permission_generation=1)
        if foreign:identity.principal_id=UUID(int=2)
        class DB:
            def execute(self,query,args=()):
                calls.append((query,args))
                if 'FROM core.records' in query and expired:clock[0]=102
                return NS(fetchall=lambda:[(ID,'binary',1,b'opaque')])
        @contextmanager
        def connection():
            calls.append('connect');yield DB();calls.append('closed')
        @contextmanager
        def authorized(*args):
            calls.append(('authorized',args[2:]));yield identity;calls.append('auth_final')
        port=c11b.C11bRecordPort(connection,ID,{'p':ID})
        scope={'method':'GET','path':'/search','headers':[(b'x-project',b'p'),(b'authorization',b'Bearer synthetic')]}
        principal=dict(tenant_id=str(ID),project_id=str(ID),principal_id=str(ID),permission_generation=1)
        return port,scope,principal,calls,clock,authorized

    def test_current_reader_uses_owned_c04_scope_bound_query_and_keeps_unknown_kind(self):
        port,scope,principal,calls,clock,auth=self.probe()
        with patch.object(c11b,'authorized',auth),patch.object(c11b.time,'monotonic',lambda:clock[0]):
            rows=port._keyword_current(principal,scope,101,101)
        self.assertEqual([(r.record_id,r.kind,r.source_revision,r.payload) for r in rows],[(str(ID),'binary',1,b'opaque')])
        query,args=next(c for c in calls if isinstance(c,tuple) and 'FROM core.records' in c[0])
        self.assertIn('r.tenant_id=%s AND r.project_id=%s',query);self.assertIn('NOT r.tombstone',query)
        self.assertIn('ORDER BY r.id LIMIT %s',query);self.assertEqual(args[:2],(ID,ID));self.assertIn(101,args)
        self.assertEqual(calls[-2:],['auth_final','closed'])

    def test_foreign_scope_or_expired_result_cannot_escape_actual_core_reader(self):
        for foreign,expired in ((True,False),(False,True)):
            port,scope,principal,calls,clock,auth=self.probe(foreign=foreign,expired=expired)
            with self.subTest(foreign=foreign,expired=expired),patch.object(c11b,'authorized',auth),patch.object(c11b.time,'monotonic',lambda:clock[0]):
                with self.assertRaises(AuthError if foreign else CapabilityUnavailable):port._keyword_current(principal,scope,1,101)
                if foreign:self.assertFalse(any(isinstance(c,tuple) and 'FROM core.records' in c[0] for c in calls))

    def test_expired_entry_or_invalid_bound_never_opens_connection(self):
        port,scope,principal,calls,clock,auth=self.probe()
        with patch.object(c11b.time,'monotonic',lambda:clock[0]):
            with self.assertRaises(CapabilityUnavailable):port._keyword_current(principal,scope,1,99)
            with self.assertRaises(Exception):port._keyword_current(principal,scope,102,101)
        self.assertEqual(calls,[])
