"""Fixed resource composition controls; driver doubles are source-only evidence."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cortex_core.runtime.bootstrap_config import decode_config
from cortex_core.runtime.custody import open_custody
from cortex_core.runtime.tests.test_bootstrap_config import sample
try:
    from cortex_core.runtime import resources
except ImportError:
    resources = None


class DriverConnection:
    def __init__(self,name,events,close_error=False):
        self.name,self.events,self.close_error=name,events,close_error
    def close(self):
        self.events.append('close:'+self.name)
        if self.close_error:raise RuntimeError('public-test-secret must not escape cleanup')


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        (self.root/'secrets').mkdir(mode=0o700);(self.root/'secrets/cortex').mkdir(mode=0o700)
        (self.root/'secrets/cortex/ca').write_bytes(b'public-test-certificate');(self.root/'secrets/cortex/ca').chmod(0o600)
        self.value=sample(self.root);self.events=[];self.calls=[]
        self.config=decode_config(json.dumps(self.value).encode(),mode='seed')
        self.deadline=time.monotonic()+60

    @contextmanager
    def vault(self,config=None):
        config=config or self.config;r,w=os.pipe()
        try:
            os.write(w,bytes(range(32)));os.close(w);w=None
            with open_custody(config,unlock_fd=r,key_id='unit-public-key') as v:
                for name in config.roles:v.import_credential(name,b'public-test-password')
                yield v
        finally:
            os.close(r)
            if w is not None:os.close(w)

    def driver(self,**kwargs):
        name=kwargs['user'];self.calls.append(kwargs);self.events.append('open:'+name)
        return DriverConnection(name,self.events)

    def require_source(self):self.assertIsNotNone(resources,'actual fixed resource module is missing')

    def test_seed_resources_bind_actual_config_deadline_and_reverse_cleanup(self):
        self.require_source()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline) as r:
                self.assertIs(r,resources.current_seed_resources(self.config,self.deadline))
                self.assertIs(r.custody,v);self.assertIs(r.bootstrap.name,self.calls[0]['user'])
                self.assertEqual(set(r.connections),{'bootstrap','installer_enrollment','api_request'})
                self.assertEqual(list(r.role_logins),list(self.value['roles']))
                self.assertNotIn('public-test-password',repr(r))
            with self.assertRaisesRegex(ValueError,'resources_unbound'):
                resources.current_seed_resources(self.config,self.deadline)
        self.assertEqual(self.events[3:],['close:cortex_api_request','close:cortex_installer_enrollment','close:cortex_bootstrap'])

    def test_serve_opens_only_api_and_probe_without_seed_context(self):
        self.require_source();config=decode_config(json.dumps(self.value).encode(),mode='serve')
        with self.vault(config) as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_core_resources(config,custody=v,deadline=self.deadline) as r:
                self.assertEqual(set(r.connections),{'api_request','probe'})
                with self.assertRaisesRegex(ValueError,'resources_unbound'):
                    resources.current_seed_resources(config,self.deadline)
        self.assertEqual([c['user'] for c in self.calls],['cortex_api_request','cortex_probe'])
        self.assertEqual(self.events[2:],['close:cortex_probe','close:cortex_api_request'])

    def test_wrong_mode_refuses_before_custody_or_driver_access(self):
        self.require_source()
        with patch.object(resources,'_driver_connect',side_effect=AssertionError('driver reached')):
            with self.assertRaisesRegex(ValueError,'resources_mode'):
                with resources.open_core_resources(self.config,custody=object(),deadline=self.deadline):pass

    def test_custody_must_be_actual_same_config_object(self):
        self.require_source();other=decode_config(json.dumps(self.value).encode(),mode='seed')
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=AssertionError('driver reached')):
            with self.assertRaisesRegex(ValueError,'resources_custody'):
                with resources.open_seed_resources(other,custody=v,deadline=self.deadline):pass
            with self.assertRaisesRegex(ValueError,'resources_custody'):
                with resources.open_seed_resources(self.config,custody=object(),deadline=self.deadline):pass

    def test_partial_open_closes_first_and_never_exposes_driver_details(self):
        self.require_source();count=0
        def fail(**kw):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('public-test-password hidden DSN')
            return self.driver(**kw)
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'resources_unavailable') as e:
                with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass
        self.assertNotIn('public-test-password',str(e.exception));self.assertEqual(self.events[-1],'close:cortex_bootstrap')

    def test_deadline_never_renews_and_invalid_values_refuse_before_connect(self):
        self.require_source()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=AssertionError('driver reached')):
            for deadline in (True,math.inf,math.nan,0,time.monotonic()-1,time.monotonic()+0.1):
                with self.subTest(deadline=deadline),self.assertRaisesRegex(ValueError,'resources_deadline'):
                    with resources.open_seed_resources(self.config,custody=v,deadline=deadline):pass

    def test_post_connect_expiry_closes_new_connection(self):
        self.require_source();now=time.monotonic()
        def connect(**kw):
            result=self.driver(**kw);clock[0]=now+61;return result
        clock=[now]
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=connect),patch.object(resources.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with resources.open_seed_resources(self.config,custody=v,deadline=now+60):pass
        self.assertEqual(self.events,['open:cortex_bootstrap','close:cortex_bootstrap'])

    def test_all_cleanup_attempted_and_failure_is_nonzero_typed(self):
        self.require_source()
        def driver(**kw):return DriverConnection(kw['user'],self.events,True)
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=driver):
            with self.assertRaisesRegex(ValueError,'resources_cleanup') as e:
                with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass
        self.assertNotIn('public-test-secret',str(e.exception));self.assertEqual(len(self.events),3)
        with self.assertRaisesRegex(ValueError,'resources_unbound'):
            resources.current_seed_resources(self.config,self.deadline)

    def test_nested_seed_context_refuses_without_replacing_outer(self):
        self.require_source()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline) as first:
                with self.assertRaisesRegex(ValueError,'resources_bound'):
                    with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass
                self.assertIs(resources.current_seed_resources(self.config,self.deadline),first)
        self.assertEqual(len(self.calls),3)

    def test_accessor_rejects_other_identity_deadline_expiry_and_closed_handles(self):
        self.require_source();other=decode_config(json.dumps(self.value).encode(),mode='seed')
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline) as r:
                for config,deadline in ((other,self.deadline),(self.config,self.deadline+1)):
                    with self.subTest(config=config.mode,deadline=deadline),self.assertRaisesRegex(ValueError,'resources_unbound'):
                        resources.current_seed_resources(config,deadline)
                with patch.object(resources.time,'monotonic',return_value=self.deadline+1):
                    with self.assertRaisesRegex(ValueError,'resources_deadline'):r.check_deadline()
            with self.assertRaisesRegex(ValueError,'resources_closed'):r.check_deadline()

    def test_fixed_connector_uses_tls_explicit_fields_and_bounded_single_attempt(self):
        self.require_source()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass
        self.assertEqual(len(self.calls),3)
        for call in self.calls:
            self.assertEqual(call['sslmode'],'verify-full');self.assertEqual(call['host'],'core.internal')
            self.assertEqual(call['dbname'],'cortex');self.assertEqual(call['gssencmode'],'disable')
            self.assertEqual(call['password'],'public-test-password');self.assertFalse(call['prepare_threshold'])
            self.assertTrue(2<=call['connect_timeout']<=10);self.assertIn('statement_timeout=',call['options'])
            self.assertNotIn('dsn',call);self.assertNotIn('conninfo',call)

    def test_private_certificate_is_rechecked_after_connect(self):
        self.require_source()
        def changed(**kw):
            result=self.driver(**kw);(self.root/'secrets/cortex/ca').write_bytes(b'different-certificate');return result
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'resources_changed'):
                with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass
        self.assertEqual(self.events,['open:cortex_bootstrap','close:cortex_bootstrap'])

    def test_handles_and_connection_map_are_immutable_and_repr_private(self):
        self.require_source()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline) as r:
                with self.assertRaises(TypeError):r.connections['migrator']=object()
                with self.assertRaises(AttributeError):r.bootstrap=object()
                self.assertNotIn('public-test-password',repr(r))

    def test_ambient_libpq_configuration_refuses_before_any_driver_attempt(self):
        self.require_source()
        with self.vault() as v,patch.dict(os.environ,{'PGHOSTADDR':'192.0.2.1'}),patch.object(resources,'_driver_connect',side_effect=AssertionError('ambient driver reached')):
            with self.assertRaisesRegex(ValueError,'resources_environment'):
                with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline):pass

    def test_blocking_connector_cannot_extend_original_absolute_deadline(self):
        self.require_source()
        def blocked(**kw):time.sleep(3);return self.driver(**kw)
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=blocked):
            started=time.monotonic()
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with resources.open_seed_resources(self.config,custody=v,deadline=started+2.1):pass
            self.assertLess(time.monotonic()-started,2.75,'connector exceeded the original absolute deadline')

    def test_fixed_driver_wrapper_preserves_absolute_timeout_refusal(self):
        self.require_source()
        class BlockingDriver:
            @staticmethod
            def connect(**kw):time.sleep(3);raise RuntimeError('late public-test-secret')
        with self.vault() as v,patch.dict('sys.modules',{'psycopg':SimpleNamespace(Connection=BlockingDriver)}):
            started=time.monotonic()
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with resources.open_seed_resources(self.config,custody=v,deadline=started+2.1):pass
            self.assertLess(time.monotonic()-started,2.75)


class CatalogConnection(DriverConnection):
    def __init__(self,name,events,rows):super().__init__(name,events);self.rows=rows;self.queries=[]
    @contextmanager
    def transaction(self):
        self.events.append('begin:'+self.name)
        try:yield
        except BaseException:self.events.append('rollback:'+self.name);raise
        else:self.events.append('commit:'+self.name)
    def execute(self,sql,params=None):
        self.queries.append((str(sql),params));self.events.append('sql:'+str(sql).splitlines()[0])
        if 'set_config' in str(sql):return SimpleNamespace(fetchone=lambda:(None,None))
        for key,rows in self.rows.items():
            if key in str(sql):return SimpleNamespace(fetchall=lambda:rows,fetchone=lambda:rows[0] if rows else None)
        if str(sql).startswith('SET TRANSACTION'):return SimpleNamespace()
        raise RuntimeError('unexpected SQL public-test-password')


class CatalogTests(unittest.TestCase):
    setUp=ResourceTests.setUp
    vault=ResourceTests.vault
    def require_source(self):
        self.assertTrue(resources is not None and hasattr(resources.SeedResources,'observe_catalog')
            and hasattr(resources.SeedResources,'transaction'),'actual catalog/transaction source missing')
    def driver(self,**kw):
        c=CatalogConnection(kw['user'],self.events,self.rows);self.calls.append(c);return c
    def data(self):
        names=list(self.config.role_logins.values())+['kaidera-runtime-core-request','kaidera-runtime-core-verifier']
        return {'FROM pg_catalog.pg_database': [('cortex',42,43)],
            'FROM pg_catalog.pg_stat_activity': [('actual-session',900,42)],
            'FROM pg_catalog.pg_roles':[(name,100+i,True,False,False,False,False,False,False) for i,name in enumerate(names)],
            'FROM pg_catalog.pg_auth_members':[(102,105,True,False,False),(999,102,False,False,True)],
            'FROM auth.runtime_login_bindings':[(102,names[2],'api_request',self.config.installation_id,True,1,
                '10000000-0000-0000-0000-000000000010',42,43,None)],
            'FROM pg_catalog.pg_namespace':[('schema','auth','auth',102),('relation','core','records',43)]}
    @contextmanager
    def opened(self):
        self.rows=self.data()
        with self.vault() as v,patch.object(resources,'_driver_connect',side_effect=self.driver):
            with resources.open_seed_resources(self.config,custody=v,deadline=self.deadline) as r:yield r
    def test_actual_catalog_is_detached_raw_observation_not_synthesized_authority(self):
        self.require_source()
        with self.opened() as r:
            observed=r.observe_catalog();self.assertEqual(set(observed),{'database','session','roles','groups','memberships','bindings','protected_owners'})
            self.assertEqual(observed['session'],{'login':'actual-session','oid':900,'database_oid':42})
            self.assertEqual(observed['database'],{'name':'cortex','oid':42,'owner_oid':43})
            self.assertEqual(observed['roles']['api_request']['oid'],102)
            self.assertNotIn('revision',observed['roles']['bootstrap'])
            self.assertEqual(observed['bindings'][0]['revision'],1)
            observed['roles']['api_request']['login']='changed'
            self.assertEqual(r.observe_catalog()['roles']['api_request']['login'],self.config.role_logins['api_request'])
    def test_incident_memberships_and_ownership_are_retained_for_sole_validator(self):
        self.require_source()
        with self.opened() as r:
            o=r.observe_catalog();self.assertIn({'member':999,'oid':102,'set':False,'inherit':False,'admin':True},o['memberships'])
            self.assertIn({'kind':'schema','schema':'auth','name':'auth','owner_oid':102},o['protected_owners'])
            self.assertEqual(o['roles']['api_request']['memberships'],[{'oid':105,'set':True,'inherit':False,'admin':False}])
            self.assertEqual(set(o['groups']),{'kaidera-runtime-core-request','kaidera-runtime-core-verifier'})
    def test_observer_uses_one_readonly_snapshot_and_bound_identity_parameters(self):
        self.require_source()
        with self.opened() as r:
            r.observe_catalog();c=self.calls[0]
            self.assertEqual(self.events.count('begin:cortex_bootstrap'),1)
            self.assertEqual(self.events.count('commit:cortex_bootstrap'),1)
            self.assertTrue(any('REPEATABLE READ READ ONLY' in s for s,p in c.queries))
            bind=[(s,p) for s,p in c.queries if 'FROM auth.runtime_login_bindings' in s][0]
            self.assertIn(' OR ',bind[0]);self.assertIn(self.config.installation_id,bind[1])
            self.assertFalse(any('INSERT ' in s or 'UPDATE ' in s or 'DELETE ' in s for s,p in c.queries))
            self.assertFalse(any(c.queries for c in self.calls[1:]))
    def test_malformed_or_failed_catalog_refuses_privately_with_rollback(self):
        self.require_source()
        with self.opened() as r:
            self.rows['FROM pg_catalog.pg_database']=[]
            with self.assertRaisesRegex(ValueError,'catalog_unavailable') as e:r.observe_catalog()
            self.assertNotIn('public-test-password',str(e.exception));self.assertIn('rollback:cortex_bootstrap',self.events)
    def test_each_transaction_refreshes_timeout_from_same_deadline(self):
        self.require_source()
        with self.opened() as r:
            now=time.monotonic()
            with patch.object(resources.time,'monotonic',return_value=now):
                with r.transaction('installer_enrollment') as c:self.assertIs(c,r.installer_enrollment)
            with patch.object(resources.time,'monotonic',return_value=now+5):
                with r.transaction('installer_enrollment'):pass
            budgets=[int(p[0]) for s,p in self.calls[1].queries if 'set_config' in s]
            self.assertEqual(len(budgets),2);self.assertGreater(budgets[0],budgets[1]+4000)
    def test_unadmitted_nested_and_closed_transactions_refuse_before_sql(self):
        self.require_source()
        with self.opened() as r:
            with self.assertRaisesRegex(ValueError,'resources_purpose'):
                with r.transaction('migrator'):pass
            with r.transaction('bootstrap'):
                with self.assertRaisesRegex(ValueError,'resources_transaction'):
                    with r.transaction('bootstrap'):pass
        with self.assertRaisesRegex(ValueError,'resources_closed'):
            with r.transaction('bootstrap'):pass
    def test_blocking_sql_cannot_extend_original_absolute_seed_deadline(self):
        self.require_source()
        with self.opened() as r:
            object.__setattr__(r,'_deadline',time.monotonic()+0.15)
            started=time.monotonic()
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with r.transaction('bootstrap'):time.sleep(1)
            self.assertLess(time.monotonic()-started,0.7)
            self.assertIn('rollback:cortex_bootstrap',self.events)


class TransactionFailureTests(unittest.TestCase):
    setUp=ResourceTests.setUp
    vault=ResourceTests.vault
    driver=CatalogTests.driver
    data=CatalogTests.data
    opened=CatalogTests.opened
    require_source=CatalogTests.require_source
    def test_failed_sql_stage_taints_carrier_until_complete_resource_close(self):
        self.require_source()
        with self.opened() as r:
            with self.assertRaisesRegex(ValueError,'resources_transaction'):
                with r.transaction('bootstrap'):raise RuntimeError('private SQL state')
            with self.assertRaisesRegex(ValueError,'resources_tainted'):
                resources.current_seed_resources(self.config,self.deadline)
            with self.assertRaisesRegex(ValueError,'resources_tainted'):
                with r.transaction('installer_enrollment'):pass
            self.assertFalse(any(c.queries for c in self.calls[1:]))
    def test_deadline_also_bounds_driver_rollback_after_body_interruption(self):
        self.require_source()
        with self.opened() as r:
            @contextmanager
            def stalled_rollback():
                try:yield
                except BaseException:time.sleep(1);raise
            self.calls[0].transaction=stalled_rollback
            object.__setattr__(r,'_deadline',time.monotonic()+0.15)
            started=time.monotonic()
            with self.assertRaisesRegex(ValueError,'resources_deadline'):
                with r.transaction('bootstrap'):time.sleep(1)
            self.assertLess(time.monotonic()-started,0.7,'rollback extended original SQL deadline')


if __name__=='__main__':unittest.main()
