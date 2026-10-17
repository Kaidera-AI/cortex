"""Fixed runtime composition. Doubles prove ownership, not live SQL admission."""
import asyncio
from contextlib import contextmanager
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from psycopg.pq import TransactionStatus
from cortex_core.api import c11b
from cortex_core.api.c11a import C05Committed
from cortex_core.auth import AuthError
from cortex_core.records import RecordError
from cortex_core.runtime import resources
from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.tests import test_resources as fixtures
try:
    from cortex_core.runtime import services
except ImportError:
    services = None

RECEIPT = dict(record_id='10000000-0000-0000-0000-000000000040', revision=1,
    event_id='10000000-0000-0000-0000-000000000041', payload_sha256='a'*64)
PRINCIPAL = dict(principal_id='10000000-0000-0000-0000-000000000004',
    project_id='10000000-0000-0000-0000-000000000003',
    tenant_id='10000000-0000-0000-0000-000000000002', permission_generation=1)
HEADERS = [(b'authorization',b'Bearer public-test-credential'),(b'x-project',b'sample'),
    (b'x-project-id',PRINCIPAL['project_id'].encode()),(b'x-agent-name',b'unit'),
    (b'idempotency-key',b'unit-request')]


class Connection(fixtures.CatalogConnection):
    def __init__(self,*args):
        super().__init__(*args);self.closed=False;self.autocommit=True
        self.info=SimpleNamespace(transaction_status=TransactionStatus.IDLE)
    def close(self):
        self.closed=True;super().close()


class ServiceTests(unittest.TestCase):
    setUp=fixtures.ResourceTests.setUp
    vault=fixtures.ResourceTests.vault
    data=fixtures.CatalogTests.data

    def require_source(self):
        self.assertTrue(services is not None and hasattr(resources.CoreResources,'request_connection'),
            'actual bounded fixed API composition is missing')

    def driver(self,**kw):
        connection=Connection(kw['user'],self.events,self.rows);self.calls.append(connection)
        return connection

    @contextmanager
    def opened(self):
        self.config=decode_config(json.dumps(self.value).encode(),mode='serve');self.rows=self.data()
        self.rows.pop('FROM auth.runtime_login_bindings')
        self.rows.update({'auth.runtime_request_installation':[(True,)],
            'auth.enrollment_readiness':[('ready',)],'SELECT 1':[(1,)]})
        with self.vault() as vault,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_core_resources(self.config,custody=vault,deadline=self.deadline) as owned:yield owned

    @contextmanager
    def bound(self,owned):
        module=ModuleType('cortex_ship.install.roles');self.policy=[];self.principals=[];self.writes=[]
        self.write_result=None;self.ledger=[]
        def validate(config,observation,*,phase):
            self.policy.append((config,observation,phase));return json.loads(json.dumps(observation))
        module.validate_catalog=validate
        def ledger(connection):self.ledger.append(connection);return True
        test=self
        def principal(port,scope):
            with port.connection_factory() as connection:
                test.principals.append((threading.get_ident(),connection,scope['_cortex_deadline']))
                c11b._credential(scope)
                return dict(PRINCIPAL)
        def memory(port,principal,scope,key):
            with port.connection_factory() as connection:
                test.writes.append((threading.get_ident(),connection,scope['_c11b_body'],key))
                return test.write_result or C05Committed(key,dict(RECEIPT),True,dict(id=RECEIPT['record_id'],created=True))
        def read(port,principal,scope,record_id):
            with port.connection_factory() as connection:
                test.assertIs(connection,owned.api_request);return dict(id=record_id,content='public-unit')
        with patch.dict(sys.modules,{'cortex_ship.install.roles':module}), \
             patch('cortex_core.schema.readiness.check_schema_ready',side_effect=ledger), \
             patch.object(c11b.C11bRecordPort,'_principal',principal), \
             patch.object(c11b.C11bRecordPort,'_write_memory',memory), \
             patch.object(c11b.C11bRecordPort,'_read_record',read):
            yield services.bind_core_services(owned)

    def request(self,bound,method='POST',path='/memory',headers=None,body=None):
        messages=[];used=False
        async def receive():
            nonlocal used
            self.assertFalse(used,'existing body parser read beyond EOF');used=True
            return dict(type='http.request',body=json.dumps(body or {'section':'unit','content':'payload'}).encode(),more_body=False)
        async def send(message):messages.append(message)
        scope=dict(type='http',method=method,path=path,query_string=b'',headers=list(HEADERS if headers is None else headers))
        asyncio.run(bound(scope,receive,send))
        self.assertEqual([m['type'] for m in messages],['http.response.start','http.response.body'])
        return messages[0]['status'],json.loads(messages[1]['body'])

    def test_startup_checks_actual_ledger_catalog_policy_and_probe_before_ready(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            self.assertEqual(self.request(bound,'GET','/health')[0],503)
            bound.start()
            self.assertEqual(self.ledger,[owned.api_request]);self.assertEqual(len(self.policy),1)
            self.assertIs(self.policy[0][0],self.config);self.assertEqual(self.policy[0][2],'serve')
            self.assertIsNone(self.policy[0][1]['bindings'])
            status,body=self.request(bound,'GET','/health')
            self.assertEqual(status,200);self.assertEqual(body['installation_id'],self.config.installation_id)
            self.assertEqual(body['readiness'],'ready');self.assertEqual(body['status'],'ok')
            self.assertTrue(any(sql=='SELECT 1' for sql,params in owned.probe.queries))

    def test_ledger_failure_never_admits_startup_or_runs_catalog_policy(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            with patch('cortex_core.schema.readiness.check_schema_ready',side_effect=RuntimeError('private-password')):
                with self.assertRaisesRegex(ValueError,'service_startup') as error:bound.start()
            self.assertNotIn('private-password',str(error.exception));self.assertEqual(self.policy,[])
            self.assertEqual(self.request(bound,'GET','/health')[0],503)

    def test_missing_or_refused_fixed_validator_never_falls_back(self):
        self.require_source()
        for replacement in (None,ModuleType('missing_validator')):
            with self.subTest(replacement=replacement),self.opened() as owned,self.bound(owned) as bound:
                with patch.dict(sys.modules,{'cortex_ship.install.roles':replacement}):
                    with self.assertRaisesRegex(ValueError,'catalog_validator_unavailable'):bound.start()
                self.assertEqual(self.request(bound,'GET','/health')[0],503)
        with self.opened() as owned,self.bound(owned) as bound:
            with patch.object(sys.modules['cortex_ship.install.roles'],'validate_catalog',side_effect=RuntimeError('private-password')):
                with self.assertRaisesRegex(ValueError,'catalog_refused'):bound.start()
            self.assertEqual(self.request(bound,'GET','/health')[0],503)

    def test_false_installation_enrollment_or_probe_cannot_admit_startup(self):
        self.require_source()
        for key,rows in [('auth.runtime_request_installation',[(False,)]),
                         ('auth.enrollment_readiness',[('enrollment_required',)]),('SELECT 1',[(0,)])]:
            with self.subTest(key=key),self.opened() as owned,self.bound(owned) as bound:
                self.rows[key]=rows
                with self.assertRaisesRegex(ValueError,'service_startup'):bound.start()
                self.assertEqual(self.request(bound,'GET','/health')[0],503)

    def test_same_c05_committed_receipt_and_existing_body_parser_reach_wire(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            bound.start();status,body=self.request(bound)
            self.assertEqual(status,200);self.assertEqual(body,dict(id=RECEIPT['record_id'],created=True,receipt=RECEIPT))
            self.assertEqual(self.writes[0][2:],({'section':'unit','content':'payload'},'unit-request'))
            self.assertIs(self.writes[0][1],owned.api_request)
            self.assertEqual(self.writes[0][0],threading.get_ident())
            self.assertTrue(all(tid==threading.get_ident() and connection is owned.api_request
                for tid,connection,deadline in self.principals))
            with self.assertRaisesRegex(ValueError,'resources_unbound'):
                with bound.port.connection_factory():pass

    def test_existing_record_reader_and_unauthenticated_wire_are_preserved(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            bound.start();status,body=self.request(bound,'GET','/records/'+RECEIPT['record_id'])
            self.assertEqual((status,body['content']),(200,'public-unit'))
            status,body=self.request(bound,headers=[h for h in HEADERS if h[0]!=b'authorization'])
            self.assertEqual(status,401);self.assertEqual(body['error']['code'],'credential_required')
            self.assertLessEqual(len(body['error']['message'].encode()),256)
            self.assertEqual(self.request(bound)[0],200,'normal 401 poisoned an IDLE reusable connection')

    def test_duplicate_authority_headers_and_missing_key_refuse_before_principal(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            bound.start()
            for header in HEADERS:
                with self.subTest(header=header[0]):self.assertEqual(self.request(bound,headers=HEADERS+[header])[0],400)
            self.assertEqual(self.request(bound,headers=[h for h in HEADERS if h[0]!=b'idempotency-key'])[0],400)
            self.assertEqual(self.principals,[]);self.assertEqual(self.writes,[])

    def test_false_wrong_key_and_malformed_receipts_never_publish_write_success(self):
        self.require_source()
        for value in [C05Committed('unit-request',RECEIPT,False,{}),C05Committed('wrong',RECEIPT,True,{}),
                      C05Committed('unit-request',{**RECEIPT,'extra':True},True,{}),
                      C05Committed('unit-request',{**RECEIPT,'revision':True},True,{}),
                      C05Committed('unit-request',{**RECEIPT,'payload_sha256':'private-password'},True,{})]:
            with self.subTest(value=value),self.opened() as owned,self.bound(owned) as bound:
                bound.start();self.write_result=value;status,body=self.request(bound)
                self.assertEqual(status,503);self.assertNotIn('private-password',json.dumps(body))

    def test_health_rechecks_protected_state_and_cannot_reuse_startup_ready(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            bound.start();self.rows['auth.enrollment_readiness']=[('enrollment_required',)]
            status,body=self.request(bound,'GET','/health')
            self.assertEqual((status,body['readiness']),(200,'enrollment_required'))
            self.rows['auth.runtime_request_installation']=[(False,)]
            self.assertEqual(self.request(bound,'GET','/health')[0],503)

    def test_requests_have_fresh_bounded_budget_after_startup_deadline_expires(self):
        self.require_source()
        with self.opened() as owned,self.bound(owned) as bound:
            bound.start();object.__setattr__(owned,'_deadline',time.monotonic()-1)
            before=time.monotonic();self.assertEqual(self.request(bound)[0],200)
            for tid,connection,deadline in self.principals:
                self.assertGreater(deadline,before);self.assertLessEqual(deadline,before+10.1)
            self.assertEqual(len({deadline for tid,connection,deadline in self.principals}),1)

    def test_only_actual_resource_owner_is_accepted(self):
        self.require_source()
        for value in (None,self.config,SimpleNamespace(connections={})):
            with self.assertRaisesRegex(ValueError,'resources_unbound'):services.bind_core_services(value)

    def test_request_connection_refuses_invalid_budget_purpose_nested_and_closed(self):
        self.require_source()
        with self.opened() as owned:
            for purpose,deadline in [('bootstrap',time.monotonic()+1),('api_request',float('inf')),
                                     ('api_request',time.monotonic()-1),('api_request',time.monotonic()+20)]:
                with self.assertRaises(ValueError):
                    with owned.request_connection(purpose,deadline=deadline):pass
            with owned.request_connection('api_request',deadline=time.monotonic()+1):
                with self.assertRaisesRegex(ValueError,'resources_transaction'):
                    with owned.request_connection('probe',deadline=time.monotonic()+1):pass
        with self.assertRaisesRegex(ValueError,'resources_closed'):
            with owned.request_connection('api_request',deadline=time.monotonic()+1):pass

    def test_normal_domain_refusal_reuses_only_actual_open_autocommit_idle_connection(self):
        self.require_source()
        with self.opened() as owned:
            for error in (AuthError('forbidden'),RecordError('conflict')):
                with self.assertRaises(type(error)):
                    with owned.request_connection('api_request',deadline=time.monotonic()+1):raise error
            with owned.request_connection('api_request',deadline=time.monotonic()+1) as connection:
                self.assertIs(connection,owned.api_request)
        with self.opened() as owned:
            owned.api_request.info.transaction_status=TransactionStatus.INERROR
            with self.assertRaisesRegex(ValueError,'resources_request'):
                with owned.request_connection('api_request',deadline=time.monotonic()+1):raise AuthError('forbidden')
            with self.assertRaisesRegex(ValueError,'resources_tainted'):
                with owned.request_connection('probe',deadline=time.monotonic()+1):pass

    def test_infrastructure_failure_and_blocked_sql_taint_without_renewing_deadline(self):
        self.require_source()
        for error in (RecordError('core_unavailable'),RuntimeError('private-password')):
            with self.subTest(error=error),self.opened() as owned:
                with self.assertRaisesRegex(ValueError,'resources_request') as caught:
                    with owned.request_connection('api_request',deadline=time.monotonic()+1):raise error
                self.assertNotIn('private-password',str(caught.exception))
                with self.assertRaisesRegex(ValueError,'resources_tainted'):
                    with owned.request_connection('probe',deadline=time.monotonic()+1):pass

    def test_oversized_request_refuses_before_any_driver_or_taint(self):
        self.require_source()
        with self.opened() as owned:
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with owned.request_connection('api_request',deadline=time.monotonic()+20):pass
            self.assertEqual(owned.api_request.queries,[])
            with owned.request_connection('api_request',deadline=time.monotonic()+1) as connection:
                self.assertIs(connection,owned.api_request)
        with self.opened() as owned:
            start=time.monotonic()
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with owned.request_connection('api_request',deadline=start+0.15):time.sleep(1)
            self.assertLess(time.monotonic()-start,0.7)


if __name__=='__main__':unittest.main()
