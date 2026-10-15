"""Source-only reviewer probes. SQL is recorded, never sent to PostgreSQL."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from cortex_core.install import owner
from cortex_core.release.codec import canonical_bytes
from cortex_core.runtime import role_grants as api
from psycopg import sql
from psycopg.pq import TransactionStatus

MATRIX=json.loads((Path(__file__).resolve().parents[1]/'runtime_grants/ratified_matrix.json').read_bytes())
ROLES={k:'hale_source_'+k for k in MATRIX['roles']}
OIDS={name:1000+i for i,name in enumerate(ROLES.values())}

class Rows:
    def __init__(self,rows=()):self.rows=list(rows)
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows

class RecordingConnection:
    """Only SQL emission/control flow; no SQL parser or database state emulator."""
    def __init__(self,*,autocommit=True,status=TransactionStatus.IDLE):
        self.autocommit=autocommit;self.info=SimpleNamespace(transaction_status=status,backend_pid=101)
        self.calls=[];self.contexts=[];self.active_rows=[]
    def execute(self,statement,parameters=None):
        text=statement if isinstance(statement,str) else statement.as_string()
        self.calls.append((text,parameters))
        if text.startswith('SELECT oid FROM pg_catalog.pg_roles'):
            return Rows([(OIDS[parameters[0]],)])
        if 'FROM pg_catalog.pg_stat_activity' in text:return Rows(self.active_rows)
        return Rows([(True,)])
    @contextmanager
    def transaction(self):
        # Psycopg's documented nesting choice; this recorder has no DB effects.
        kind='BEGIN_COMMIT' if self.info.transaction_status==TransactionStatus.IDLE else 'SAVEPOINT_RELEASE'
        self.contexts.append(kind)
        previous=self.info.transaction_status
        self.info.transaction_status=TransactionStatus.INTRANS
        try:yield SimpleNamespace(savepoint_name=None if previous==TransactionStatus.IDLE else '_pg3_1')
        finally:self.info.transaction_status=previous


def isolated_adapter(conn):
    a=object.__new__(api._RoleAdapter);a.roles=dict(ROLES);a.database=(55,'hale_source_db',OIDS[ROLES['bootstrap']],True,-1)
    a.connection=conn;a.recovery=RecordingConnection();a.created={};a.backends={101:(OIDS[ROLES['bootstrap']],'fixed-start',55)}
    a._current=lambda *args:{'role':canonical_bytes({'roles':[],'memberships':[]})}
    a.admit=lambda c:None;a.receipt=lambda:{'source_only':True}
    return a

class SQLControlEmission(unittest.TestCase):
    def test_create_absent_role_emits_nologin(self):
        conn=RecordingConnection();a=isolated_adapter(conn)
        present=[]
        for logical,name in ROLES.items():
            if logical=='probe':continue
            present.append([OIDS[name],name,False,False,False,False,False,False,False,-1,None,None])
        a._roles=lambda c:{'roles':present,'memberships':[]}
        a._known_oids=lambda:{r[1]:r[0] for r in present}
        a.prepare(conn)
        emitted=[s for s,p in conn.calls if s.startswith('CREATE ROLE ')]
        self.assertEqual(len(emitted),1)
        self.assertIn(' NOLOGIN NOSUPERUSER ',emitted[0], 'SQL_CREATE_LOGIN_BOUNDARY')

    def test_close_emits_finite_nologin_nosuperuser(self):
        conn=RecordingConnection();a=isolated_adapter(conn)
        conn.active_rows=[(101,*a.backends[101])]
        a.close(conn)
        emitted=[s for s,p in conn.calls if s.startswith('ALTER ROLE ')]
        self.assertEqual(len(emitted),2)
        for statement in emitted:
            self.assertIn(' NOLOGIN NOSUPERUSER ',statement,'SQL_CLOSE_DEMOTION_BOUNDARY')

class TransactionOwnership(unittest.TestCase):
    def test_idle_autocommit_control_owns_transaction(self):
        conn=RecordingConnection();a=isolated_adapter(conn);body=[]
        with a._transaction(conn):body.append('effect')
        self.assertEqual(conn.contexts,['BEGIN_COMMIT'])
        self.assertEqual(body,['effect'])

    def test_ambient_transaction_is_refused_before_effect(self):
        conn=RecordingConnection(autocommit=False,status=TransactionStatus.INTRANS)
        a=isolated_adapter(conn);body=[]
        try:
            with a._transaction(conn):body.append('effect')
        except owner.OwnerRefusal:
            self.assertEqual(body,[])
        else:
            self.fail('PG_B2_AMBIENT_TRANSACTION_ADMITTED:'+repr({'contexts':conn.contexts,'body':body}))


class ACLCatalogue:
    """Predetermined catalogue rows/privilege answers; no SQL interpretation."""
    def __init__(self,a,*,ledger_grant=False):
        self.autocommit=True;self.info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        a.connection=a.recovery=self
        self.a=a;self.calls=[];self.ledger=MATRIX['control_ledger']['name']
        self.inventory={}
        for i,(kind,name) in enumerate(sorted(a.expected_inventory),2000):self.inventory[name]=i
        self.truth=set()
        for table in MATRIX['tables']:
            for logical in api._SERVICES:
                for privilege in table['direct_privileges'][logical]:self.truth.add((ROLES[logical],'TABLE',table['name'],privilege))
        for fn in MATRIX['functions']:
            for logical in api._SERVICES:
                for privilege in fn['direct_privileges'][logical]:self.truth.add((ROLES[logical],'FUNCTION',fn['signature'],privilege))
        for kind,key in [('TABLE','view'),('SEQUENCE','sequence'),('TYPE','type')]:
            for logical in api._SERVICES:
                for privilege in MATRIX[key]['direct_privileges'][logical]:self.truth.add((ROLES[logical],kind,MATRIX[key]['name'],privilege))
        for logical in api._SERVICES:
            self.truth.add((ROLES[logical],'DATABASE',a.database[1],'CONNECT'))
            for entry in MATRIX['schemas'].get(logical,[]):
                words=entry.split()
                if words[0]=='USAGE':self.truth.add((ROLES[logical],'SCHEMA',words[1],'USAGE'))
        if ledger_grant:self.truth.add((ROLES['api_request'],'TABLE',self.ledger,'SELECT'))
        # Keep ACL rows consistent with the independently supplied effective
        # answers, including explicit no-PUBLIC finite creator defaults.
        codes={'SELECT':'r','INSERT':'a','UPDATE':'w','DELETE':'d','TRUNCATE':'D',
               'REFERENCES':'x','TRIGGER':'t','MAINTAIN':'m','EXECUTE':'X',
               'USAGE':'U','CREATE':'C','CONNECT':'c','TEMPORARY':'T'}
        objects=[]
        inventory=set(a.expected_inventory)|{('DATABASE',a.database[1])}|{('SCHEMA',x) for x in (*api._SCHEMAS,'public')}
        for i,(kind,name) in enumerate(sorted(inventory),2000):
            self.inventory[name]=i
            actual_kind='SEQUENCE' if kind=='TABLE' and name==MATRIX['sequence']['name'] else kind
            entries=[]
            for logical in api._SERVICES:
                privileges=sorted(p for role,k,n,p in self.truth if role==ROLES[logical] and k==actual_kind and n==name)
                if privileges:entries.append(ROLES[logical]+'='+''.join(codes[p] for p in privileges)+'/'+ROLES['migrator'])
            relkind='S' if actual_kind=='SEQUENCE' else 'v' if kind=='TABLE' and name==MATRIX['view']['name'] else 'r' if kind=='TABLE' else ''
            objects.append([kind,i,OIDS[ROLES['migrator']],entries,name,relkind])
        defaults=[[OIDS[ROLES[creator]],0,kind,[ROLES[creator]+'='+code+'/'+ROLES[creator]]]
                  for creator in ('bootstrap','migrator') for kind,code in [('f','X'),('T','U')]]
        self.acl={'objects':objects,'defaults':defaults}
    def execute(self,statement,parameters=None):
        text=statement if isinstance(statement,str) else statement.as_string();self.calls.append((text,parameters))
        if 'WITH ORDINALITY AS q(' in text:
            roles,kinds,names,privileges=parameters
            return Rows([(i,(role,kind,name,privilege) in self.truth,False)
                         for i,(role,kind,name,privilege) in enumerate(zip(roles,kinds,names,privileges),1)])
        if any(x in text for x in ('to_regprocedure(%s)','to_regtype(%s)','to_regclass(%s)')):return Rows([(self.inventory[parameters[0]],)])
        if 'WHERE grantee=0' in text:return Rows()  # This catalogue has no PUBLIC grants.
        if text.startswith('SELECT oid FROM pg_catalog.pg_roles'):return Rows([(OIDS[parameters[0]],)])
        if text.startswith('SELECT 1 FROM pg_catalog.pg_database'):return Rows()  # No foreign DB ownership/grants.
        if text.startswith('SELECT g.oid,m.oid'):
            return Rows([(OIDS[ROLES['request_group']],OIDS[ROLES['api_request']])])
        raise AssertionError('UNEXPECTED_CATALOG_QUERY:'+text)


def acl_adapter():
    a=object.__new__(api._RoleAdapter);a.roles=dict(ROLES);a.policy=MATRIX;a.database=(55,'hale_source_db',OIDS[ROLES['migrator']],True,-1)
    a.expected_inventory={('FUNCTION',r['signature']) for r in MATRIX['functions']}
    a.expected_inventory|={('TABLE',r['name']) for r in MATRIX['tables']}
    a.expected_inventory|={('TABLE',MATRIX[k]['name']) for k in ('view','sequence','control_ledger')}
    a.expected_inventory|={('TYPE',MATRIX['type']['name'])}
    edge=[OIDS[ROLES['request_group']],OIDS[ROLES['api_request']],OIDS[ROLES['bootstrap']],False,False,True]
    a._roles=lambda c:{'roles':[],'memberships':[edge]};a._acl=lambda c:c.acl
    return a

class ServiceACL(unittest.TestCase):
    def test_clean_catalogue_control_is_accepted(self):
        a=acl_adapter();conn=ACLCatalogue(a);a.validate_acl(conn)

    def test_explicit_service_select_on_control_ledger_is_denied(self):
        a=acl_adapter();conn=ACLCatalogue(a,ledger_grant=True)
        self.assertEqual(MATRIX['control_ledger']['service_privileges'],[])
        try:a.validate_acl(conn)
        except owner.OwnerRefusal:return
        asked=[name for text,params in conn.calls if 'WITH ORDINALITY AS q(' in text for name in params[2]]
        self.fail('PG_B2_CONTROL_LEDGER_GRANT_ADMITTED:'+repr({'ledger':conn.ledger,'ledger_privilege_queried':conn.ledger in asked}))

class NullIdentityPreflight(unittest.TestCase):
    def acl_case(self, change):
        conn=RecordingConnection();a=isolated_adapter(conn)
        row=['TABLE',222,OIDS[ROLES['migrator']],None,'core.schema_migrations','r']
        original={'objects':[row],'defaults':[]}
        actual=list(row);change(actual)
        a._acl=lambda c:{'objects':[actual],'defaults':[]}
        return a,conn,original

    def test_equal_null_acl_is_untouched(self):
        a,conn,original=self.acl_case(lambda row:None)
        a._restore_acl(conn,original);self.assertEqual(conn.calls,[])

    def test_null_acl_representation_drift_typed_refusal_before_acl_sql(self):
        a,conn,original=self.acl_case(lambda row:row.__setitem__(3,[]))
        with self.assertRaises(owner.OwnerRefusal):a._restore_acl(conn,original)
        self.assertEqual(conn.calls,[])

    def test_object_oid_drift_typed_refusal_before_acl_sql(self):
        a,conn,original=self.acl_case(lambda row:row.__setitem__(1,333))
        with self.assertRaises(owner.OwnerRefusal):a._restore_acl(conn,original)
        self.assertEqual(conn.calls,[])

    def test_object_owner_drift_typed_refusal_before_acl_sql(self):
        a,conn,original=self.acl_case(lambda row:row.__setitem__(2,9999))
        with self.assertRaises(owner.OwnerRefusal):a._restore_acl(conn,original)
        self.assertEqual(conn.calls,[])

    def test_foreign_role_oid_typed_refusal_before_role_sql(self):
        conn=RecordingConnection();a=isolated_adapter(conn)
        rows=[[OIDS[name],name,False,False,False,False,False,False,False,-1,None,None]
              for name in ROLES.values()]
        a._roles=lambda c:{'roles':rows,'memberships':[]}
        a._known_oids=lambda:{name:OIDS[name]+10000 for name in ROLES.values()}
        with self.assertRaises(owner.OwnerRefusal):a.prepare(conn)
        self.assertFalse(any(text.startswith(('CREATE ','ALTER ','GRANT ','REVOKE ','DROP ')) for text,params in conn.calls))
