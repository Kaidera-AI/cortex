"""X3 source-policy witnesses; fixtures cannot qualify custody, SQL or native IO."""
import asyncio
from dataclasses import replace
import copy
import importlib
import pickle
import ssl
import unittest
from unittest.mock import patch
from uuid import UUID

REF, INSTALL = str(UUID(int=101)), str(UUID(int=102))
VALUE = 'fixture-only-auth-material'
PROCESS = ('host', 'boot', '1000', 'namespace', 'code-original', 'process-lifetime')


class Inputs:
    """Scripted protected-port observations; no OS, driver, file or DB operation."""
    def __init__(self, api):
        self.api = api
        self.revision, self.generation, self.epoch = 'r1', 1, 'owner-epoch'
        self.process, self.active = PROCESS, True
        self.events, self.connections = [], []
        self.observe_hook = self.read_hook = self.connect_hook = None
        self.carrier_fault = self.cleanup_fault = self.session_fault = None
        self.driver = 'psycopg'
        self.delay = 0
        self.state = None

    async def observe(self, reference, purpose, installation, mode, deadline):
        self.events.append('observe')
        if self.observe_hook: self.observe_hook(self)
        if self.delay: await asyncio.sleep(self.delay)
        if not self.active: raise RuntimeError(VALUE)
        return self.api._Grant(reference, self.revision, installation, purpose, mode,
            self.process, self.epoch, self.generation,
            min(deadline, asyncio.get_running_loop().time()+10), 'custodian-original')

    async def read(self, grant, deadline):
        self.events.append('read')
        if self.read_hook: self.read_hook(self)
        return VALUE

    async def reserve_carrier(self, grant, deadline):
        self.events.append('reserve')
        self.state = self.api._CarrierState('resource-original', ('mount','parent','inode'),
            PROCESS, '1000', 0o600, 0o700, 1, True, True, False, 0, 'S02')
        return self.state

    async def create_carrier(self, state, material, deadline):
        self.events.append('create')
        assert material == VALUE
        self.state = replace(state, present=True)
        if self.carrier_fault: self.state = replace(self.state, **self.carrier_fault)

    async def observe_carrier(self, state, deadline):
        self.events.append('carrier-observe')
        return self.state

    async def close_carrier(self, state, deadline):
        self.events.append('carrier-close')
        self.state = replace(self.state, present=False, consumers=0)
        if self.cleanup_fault: self.state = replace(self.state, **self.cleanup_fault)

    def sql_policy(self, grant):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.hostname_checks_common_name = False
        return self.api._SQLPolicy(self.driver, '10.7.0.2', 5432, 'fixture-db',
            'fixture-login', '/admitted/ca.pem', '10.7.0.2', context, 44, 7)

    async def reserve_connect(self, grant, policy, deadline):
        self.events.append('connect-reserve')
        return self.api._ConnectAttempt('operation-original','attempt-original',
                                        'owned-connect-resource',grant.process)

    async def connect(self, policy, kwargs, deadline, attempt):
        self.events.append('connect')
        self.kwargs, self.connect_deadline = kwargs.copy(), deadline
        if self.driver == 'asyncpg':
            self.password_callback = kwargs['password']
            assert await self.password_callback() == VALUE
        else: assert kwargs['password'] == VALUE
        connection = Connection();self.connections.append(connection)
        if self.connect_hook: self.connect_hook(self)
        return connection

    async def observe_session(self, connection, grant, policy, deadline):
        self.events.append('session-observe')
        result = self.api._SessionState(INSTALL, 'fixture-db', '10.7.0.2',
            '10.7.0.2', 44, 7, True, True, True)
        return replace(result, **(self.session_fault or {}))

    async def retire(self, connection, deadline):
        self.events.append('retire'); await connection.close()
        return not self.cleanup_fault


class Connection:
    def __init__(self): self.closed = False
    async def close(self): self.closed = True


class X3(unittest.IsolatedAsyncioTestCase):
    def api(self):
        try: return importlib.import_module('cortex_core.runtime.secret_resolver')
        except ModuleNotFoundError as error:
            if error.name != 'cortex_core.runtime.secret_resolver': raise
            self.fail('X3_SURFACE_ABSENT: fixed resolver source boundary')

    def fixture(self, purpose='api_request', mode='api'):
        api=self.api(); inputs=Inputs(api)
        self.addCleanup(patch.stopall)
        patch.object(api,'_installed_inputs',return_value=inputs).start()
        return api, inputs, api.SecretResolver(), purpose, mode

    def deadline(self, seconds=1): return asyncio.get_running_loop().time()+seconds

    async def handle(self, resolver, purpose='api_request', mode='api', deadline=None):
        return await resolver.resolve(REF,purpose,INSTALL,mode,self.deadline() if deadline is None else deadline)

    async def denied(self, action, reason=None, hold=False):
        api=self.api()
        with self.assertRaises(api.SecretRefusal) as caught: await action
        self.assertNotIn(VALUE,str(caught.exception))
        if reason: self.assertEqual(caught.exception.reason,reason)
        if hold: self.assertEqual(caught.exception.result,'HOLD')
        return caught.exception

    async def test_x301_installed_port_cannot_be_bound_by_uuid_or_callback(self):
        api=self.api()
        await self.denied(self.handle(api.SecretResolver()),'required_binding_gap')
        with self.assertRaises(TypeError): api.SecretResolver(lambda: VALUE)

    async def test_x301_forgery_copy_pickle_and_closed_handle_refuse(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
        self.assertEqual((h.reference,h.revision),(REF,'r1'))
        self.assertNotIn(VALUE,repr(h));self.assertNotIn('process-lifetime',repr(h))
        for fn in (copy.copy,copy.deepcopy,pickle.dumps):
            with self.assertRaises(TypeError):fn(h)
        for fake in (object.__new__(type(h)), {'reference':REF}, REF):
            await self.denied(resolver.check(fake,self.deadline()),'invalid_handle')
        h.close();h.close()
        await self.denied(resolver.check(h,self.deadline()),'invalid_handle')

    async def test_x301_process_install_purpose_mode_and_revision_are_bound(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
        for attr,value in [('process',PROCESS[:-1]+('new-lifetime',)),('revision','r2'),('epoch','new-owner'),('generation',2)]:
            original=getattr(inputs,attr);setattr(inputs,attr,value)
            await self.denied(resolver.check(h,self.deadline()),'authority_changed')
            setattr(inputs,attr,original)
        await resolver.check(h,self.deadline())
        # The original process view is closed even when a supplier would echo args.
        for purpose,mode in [('bootstrap','api'),('provider','inspect'),('api_request','core-server'),('request_group','api'),('verifier_owner','api')]:
            await self.denied(self.handle(resolver,purpose,mode),'purpose_not_in_view')
        for index,changed in [(0,str(UUID(int=200))),(2,str(UUID(int=201))),(3,'inspect')]:
            original=inputs.observe
            async def mismatched(*args):
                grant=await original(*args)
                return replace(grant,**{['reference','purpose','installation','mode'][index]:changed})
            inputs.observe=mismatched
            await self.denied(self.handle(resolver),'authority_changed')
            inputs.observe=original

    async def test_x302_revoked_and_unavailable_are_closed_without_material_read(self):
        api,inputs,resolver,_,_=self.fixture();inputs.active=False
        await self.denied(self.handle(resolver),'resolver_unavailable')
        self.assertNotIn('read',inputs.events)

    async def test_x302_deadline_and_bad_reference_refuse_before_lookup(self):
        api,inputs,resolver,_,_=self.fixture()
        for deadline in [self.deadline(-1),float('nan'),float('inf'),True]:
            await self.denied(resolver.resolve(REF,'probe',INSTALL,'api',deadline))
        for ref in ['not-uuid',REF.upper()+' ',None]:
            await self.denied(resolver.resolve(ref,'probe',INSTALL,'api',self.deadline()))
        self.assertEqual(inputs.events,[])

    async def test_x302_expired_grant_refuses(self):
        api,inputs,resolver,_,_=self.fixture();original=inputs.observe
        async def expired(*args):return replace(await original(*args),expires=self.deadline(-1))
        inputs.observe=expired
        await self.denied(self.handle(resolver),'deadline_expired')

    async def test_x303_psycopg_exact_parameters_and_rc1_protocol(self):
        api,inputs,resolver,_,_=self.fixture()
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=150)
        k=inputs.kwargs
        self.assertEqual(k,dict(conninfo='',host='10.7.0.2',hostaddr='10.7.0.2',port=5432,
            dbname='fixture-db',user='fixture-login',password=VALUE,sslmode='verify-full',
            sslrootcert='/admitted/ca.pem',gssencmode='disable',autocommit=True,
            connect_timeout=1,options='-c statement_timeout=150'))
        self.assertFalse(conn.closed);await adapter.check(conn,deadline=self.deadline())
        await adapter.retire(conn,deadline=self.deadline());self.assertTrue(conn.closed)
        self.assertNotIn(VALUE,repr(adapter))

    async def test_x303_asyncpg_strict_context_and_current_password_callback(self):
        api,inputs,resolver,_,_=self.fixture();inputs.driver='asyncpg'
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=150)
        k=inputs.kwargs;self.assertIsNone(k['dsn']);self.assertEqual(k['host'],'10.7.0.2')
        self.assertEqual(k['server_settings'],{'statement_timeout':'150'})
        self.assertNotIn('sslmode',k);self.assertNotIn('hostaddr',k)
        self.assertEqual(k['ssl'].verify_mode,ssl.CERT_REQUIRED);self.assertTrue(k['ssl'].check_hostname)
        self.assertFalse(k['ssl'].hostname_checks_common_name)
        self.assertEqual(await inputs.password_callback(),VALUE)
        inputs.active=False
        await self.denied(inputs.password_callback())
        await adapter.retire(conn,deadline=self.deadline())

    async def test_x303_tls_hba_login_oid_database_session_faults_retire(self):
        api,inputs,resolver,_,_=self.fixture()
        from cortex_core.runtime.connections import ResolverConnection
        for fault in [{'tls':False},{'hba':False},{'ip_san':'10.7.0.9'},{'login_oid':45},{'binding_revision':8},{'database':'wrong'},{'idle':False}]:
            inputs.session_fault=fault
            adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
            await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),'session_mismatch')
            self.assertTrue(inputs.connections[-1].closed)

    async def test_x304_carrier_private_finite_reserve_write_observe_close(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        carrier=await resolver.carrier(h,self.deadline())
        self.assertEqual(carrier.path,'/run/cortex/bootstrap/initdb-password')
        descriptor=carrier.descriptor
        self.assertNotIn(VALUE,repr(descriptor));self.assertNotIn('password',repr(descriptor))
        self.assertLess(inputs.events.index('reserve'),inputs.events.index('create'))
        await carrier.check();await carrier.close();await carrier.close()
        self.assertFalse(inputs.state.present);self.assertEqual(inputs.state.consumers,0)

    async def test_x304_bad_mode_identity_owner_links_or_mount_refuses_and_closes(self):
        api,inputs,resolver,_,_=self.fixture()
        for fault in [{'mode':0o644},{'directory_mode':0o755},{'uid':'other'},{'links':2},{'private':False},{'regular':False},{'identity':('other','parent','inode')}]:
            inputs.carrier_fault=fault;h=await self.handle(resolver,'bootstrap','core-init')
            await self.denied(resolver.carrier(h,self.deadline()))
            self.assertIn('carrier-close',inputs.events)
            inputs.carrier_fault=None

    async def test_x304_cleanup_survivor_is_hold_without_new_lookup(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        carrier=await resolver.carrier(h,self.deadline());inputs.active=False
        before=inputs.events.count('observe');inputs.cleanup_fault={'consumers':1}
        await self.denied(carrier.close(),'cleanup_unproved',hold=True)
        self.assertEqual(inputs.events.count('observe'),before)
        inputs.cleanup_fault=None;await carrier.close()

    async def test_x305_secret_does_not_enter_handle_carrier_descriptor_or_error(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        carrier=await resolver.carrier(h,self.deadline())
        self.assertNotIn(VALUE,repr((h,carrier,carrier.descriptor)))
        await carrier.close();inputs.active=False
        error=await self.denied(resolver.check(h,self.deadline()))
        self.assertNotIn(VALUE,repr(error))
        self.assertNotIn(VALUE,str(error))

    async def test_x307_checkout_and_publication_recheck_current_revision(self):
        api,inputs,resolver,_,_=self.fixture()
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'api_request',INSTALL,'api',self.deadline())
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
        inputs.generation+=1
        await self.denied(adapter.check(conn,deadline=self.deadline()),'authority_changed')
        self.assertTrue(conn.closed)

    async def test_x308_rotation_during_material_lookup_dispatches_no_driver(self):
        api,inputs,resolver,_,_=self.fixture();inputs.read_hook=lambda x:setattr(x,'revision','r2')
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),'authority_changed')
        self.assertNotIn('connect',inputs.events)

    async def test_x308_revocation_during_connect_withholds_and_retires(self):
        api,inputs,resolver,_,_=self.fixture();inputs.connect_hook=lambda x:setattr(x,'active',False)
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100))
        self.assertTrue(inputs.connections[-1].closed)

    async def test_x310_hung_lookup_uses_original_deadline_no_retry(self):
        api,inputs,resolver,_,_=self.fixture();inputs.delay=1
        start=asyncio.get_running_loop().time()
        await self.denied(self.handle(resolver,deadline=self.deadline(.02)),'deadline_expired')
        self.assertLess(asyncio.get_running_loop().time()-start,.3)
        self.assertEqual(inputs.events,['observe'])

    async def test_x310_cancellation_after_carrier_create_cleans_original(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        original=inputs.create_carrier;entered=asyncio.Event()
        async def hanging(*args):await original(*args);entered.set();await asyncio.Event().wait()
        inputs.create_carrier=hanging
        task=asyncio.create_task(resolver.carrier(h,self.deadline()))
        await asyncio.wait_for(entered.wait(),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(inputs.state.present);self.assertIn('carrier-close',inputs.events)

    async def test_x310_uncertain_session_cleanup_is_hold(self):
        api,inputs,resolver,_,_=self.fixture();inputs.session_fault={'tls':False};inputs.cleanup_fault=True
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),'cleanup_unproved',hold=True)

    async def test_x311_new_owner_epoch_requires_new_handle_not_old_revalidation(self):
        api,inputs,resolver,_,_=self.fixture();old=await self.handle(resolver)
        inputs.epoch='restart-owner'
        await self.denied(resolver.check(old,self.deadline()),'authority_changed')
        fresh=await self.handle(resolver);await resolver.check(fresh,self.deadline())
        old.close();fresh.close()


    async def test_x301_public_check_does_not_export_supplier_or_grant(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
        self.assertIsNone(await resolver.check(h,self.deadline()),'X3_SUPPLIER_EXPORTED')

    async def test_x304_revoked_before_carrier_handoff_refuses_but_cleanup_works(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        carrier=await resolver.carrier(h,self.deadline());inputs.active=False
        await self.denied(carrier.check())
        await carrier.close();self.assertFalse(inputs.state.present)

    async def test_x303_supplier_policy_exception_is_scrubbed(self):
        api,inputs,resolver,_,_=self.fixture()
        def broken(grant):raise RuntimeError(VALUE)
        inputs.sql_policy=broken
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        error=None
        try:await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
        except Exception as caught:error=caught
        self.assertIsInstance(error,api.SecretRefusal,'X3_RAW_POLICY_ERROR')
        self.assertNotIn(VALUE,str(error))
        self.assertNotIn('connect',inputs.events)

    async def test_x310_failed_retirement_fences_password_callback(self):
        api,inputs,resolver,_,_=self.fixture();inputs.driver='asyncpg'
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
        inputs.cleanup_fault=True
        await self.denied(adapter.retire(conn,deadline=self.deadline()),'cleanup_unproved',hold=True)
        await self.denied(inputs.password_callback(),'invalid_handle')
        inputs.cleanup_fault=None;await adapter.retire(conn,deadline=self.deadline())

    async def test_x310_lost_connect_result_reconciles_reserved_attempt_or_hold(self):
        api,inputs,resolver,_,_=self.fixture();inputs.aborted=[]
        original=inputs.connect
        async def lost(*args):
            await original(*args)
            raise RuntimeError(VALUE)
        async def abort(attempt,deadline):
            inputs.aborted.append((attempt.attempt,attempt.resource))
            for conn in inputs.connections:await conn.close()
            return True
        inputs.connect=lost;inputs.abort_connect=abort
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100))
        self.assertEqual(inputs.aborted,[('attempt-original','owned-connect-resource')],'X3_LOST_CONNECT_NOT_RECONCILED')
        self.assertTrue(inputs.connections[-1].closed)

    async def test_x310_missing_unknown_connect_cleanup_is_hold(self):
        api,inputs,resolver,_,_=self.fixture()
        async def lost(*args):raise RuntimeError(VALUE)
        inputs.connect=lost
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),'cleanup_unproved',hold=True)


    async def test_x310_cleanup_that_swallows_cancel_cannot_publish_late_success(self):
        api,inputs,resolver,_,_=self.fixture()
        from cortex_core.runtime.connections import _retire_resolved
        async def late(connection,deadline):
            try: await asyncio.sleep(1)
            except asyncio.CancelledError: return True
        inputs.retire=late
        await self.denied(_retire_resolved(inputs,Connection(),self.deadline(.01)),
                          'cleanup_unproved',hold=True)

    async def test_x304_descriptor_retains_original_phase_presence_and_expiry(self):
        api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver,'bootstrap','core-init')
        carrier=await resolver.carrier(h,self.deadline())
        descriptor=carrier.descriptor
        self.assertIsInstance(descriptor,dict,'X3_DESCRIPTOR_SHAPE')
        self.assertEqual(descriptor['phase'],'S02')
        self.assertIs(descriptor['reserved_present'],False)
        self.assertEqual(descriptor['mode'],'core-init')
        self.assertEqual(descriptor['generation'],1)
        self.assertGreater(descriptor['expires'],asyncio.get_running_loop().time())
        await carrier.close()
        self.assertEqual(carrier.descriptor,descriptor,'original reservation must not be rewritten')


    async def test_x302_operation_budget_does_not_reissue_or_shorten_owner_expiry(self):
        api,inputs,resolver,_,_=self.fixture();original=inputs.observe
        expiry=self.deadline(2);owner=self.deadline(.15)
        async def fixed(*args): return replace(await original(*args),expires=expiry)
        inputs.observe=fixed
        try:h=await self.handle(resolver,deadline=owner)
        except api.SecretRefusal:self.fail('X3_OWNER_EXPIRY_NOT_CLAMPED')
        await resolver.check(h,self.deadline(.01))
        await asyncio.sleep(.02)
        await resolver.check(h,owner)
        await asyncio.sleep(.15)
        await self.denied(resolver.check(h,self.deadline(1)),'deadline_expired')


    async def test_x305_supplier_read_policy_and_retire_logs_are_private(self):
        import logging
        api,inputs,resolver,_,_=self.fixture()
        original_read,original_policy,original_retire=inputs.read,inputs.sql_policy,inputs.retire
        async def read(*args):
            logging.getLogger('x3-fixture-read').warning('%s',VALUE)
            return await original_read(*args)
        def policy(*args):
            logging.getLogger('x3-fixture-policy').warning('%s',VALUE)
            return original_policy(*args)
        async def retire(*args):
            logging.getLogger('psycopg').warning('%s',VALUE,exc_info=True)
            return await original_retire(*args)
        inputs.read,inputs.sql_policy,inputs.retire=read,policy,retire
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        with self.assertLogs(level='WARNING') as captured:
            conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
            await adapter.retire(conn,deadline=self.deadline())
        self.assertEqual({record.name for record in captured.records},
            {'x3-fixture-read','x3-fixture-policy','psycopg'})
        self.assertFalse(any(VALUE in line for line in captured.output),'X3_SUPPLIER_LOG_LEAK')
        self.assertTrue(all(record.exc_info is None for record in captured.records))


    async def test_x308_revocation_during_reservation_dispatches_no_driver(self):
        api,inputs,resolver,_,_=self.fixture();original=inputs.reserve_connect
        async def reserve(*args):
            ticket=await original(*args);inputs.active=False;return ticket
        inputs.reserve_connect=reserve
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100))
        self.assertNotIn('connect',inputs.events,'X3_REVOKED_RESERVATION_DISPATCHED')

    async def test_x307_current_role_binding_change_withholds_and_retires(self):
        api,inputs,resolver,_,_=self.fixture();original=inputs.sql_policy
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
        inputs.sql_policy=lambda grant:replace(original(grant),binding_revision=8)
        await self.denied(adapter.check(conn,deadline=self.deadline()),'authority_changed')
        self.assertTrue(conn.closed)

    async def test_x303_insecure_asyncpg_context_never_dispatches(self):
        api,inputs,resolver,_,_=self.fixture();inputs.driver='asyncpg';original=inputs.sql_policy
        from cortex_core.runtime.connections import ResolverConnection
        for common_name,no_verification in [(True,False),(False,True)]:
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.hostname_checks_common_name=common_name
            if no_verification:
                context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
            inputs.sql_policy=lambda grant:replace(original(grant),ssl_context=context)
            adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
            await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),
                              'invalid_connection_policy')
        self.assertNotIn('connect',inputs.events)


    async def test_x304_invalid_original_reservation_dispatches_no_material_or_cleanup(self):
        api,inputs,resolver,_,_=self.fixture();original=inputs.reserve_carrier
        for field,value in [('phase','S03'),('consumers',1)]:
            async def reserve(*args):
                state=await original(*args);object.__setattr__(state,field,value);return state
            inputs.reserve_carrier=reserve
            h=await self.handle(resolver,'bootstrap','core-init')
            await self.denied(resolver.carrier(h,self.deadline()),'carrier_mismatch')
            for event in ('read','create','carrier-close'):
                self.assertNotIn(event,inputs.events,'X3_INVALID_RESERVATION_DISPATCHED')


    def park_observation(self, inputs, when):
        """Capture the observation before yielding, as in Vera's race witnesses."""
        original=inputs.observe;entered=asyncio.Event();release=asyncio.Event();count=0
        async def observe(*args):
            nonlocal count
            result=await original(*args);count+=1
            if when(count):
                entered.set();await release.wait()
            return result
        inputs.observe=observe
        return entered,release

    async def test_x3close001_waiting_check_refuses_closed_handle(self):
        api,inputs,resolver,_,_=self.fixture();e=self.deadline(2)
        h=await self.handle(resolver,'probe',deadline=e)
        entered,release=self.park_observation(inputs,lambda count:count==1)
        pending=asyncio.create_task(resolver.check(h,e))
        try:
            await asyncio.wait_for(entered.wait(),1)
            h.close()
            with self.assertRaises(api.SecretRefusal):h.reference
        finally:release.set()
        await self.denied(pending,'invalid_handle')
        await self.denied(resolver.check(h,e),'invalid_handle')

    async def test_x3close001_final_material_observation_refuses_closed_handle(self):
        api,inputs,resolver,_,_=self.fixture();e=self.deadline(2)
        h=await self.handle(resolver,'probe',deadline=e)
        entered,release=self.park_observation(inputs,lambda count:count==3)
        async def material():
            async with resolver._material(h,e) as value:return value
        pending=asyncio.create_task(material())
        try:
            await asyncio.wait_for(entered.wait(),1)
            h.close()
            with self.assertRaises(api.SecretRefusal):h.reference
        finally:release.set()
        await self.denied(pending,'invalid_handle')
        # The authentic restoration control must still deliver material.
        fresh=await self.handle(resolver,'probe',deadline=e)
        async with resolver._material(fresh,e) as value:self.assertTrue(value==VALUE)
        fresh.close()

    async def test_x3close001_pre_dispatch_observation_never_connects_after_close(self):
        api,inputs,resolver,_,_=self.fixture();holder={}
        original=api.SecretResolver.resolve
        async def resolve(self,*args):
            handle=await original(self,*args);holder['handle']=handle;return handle
        def close_during_pre_dispatch_observe(port):
            if 'connect-reserve' in port.events:
                holder['handle'].close();port.events.append('handle-closed')
        inputs.observe_hook=close_during_pre_dispatch_observe
        from cortex_core.runtime.connections import ResolverConnection
        with patch.object(api.SecretResolver,'resolve',resolve):
            adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
            await self.denied(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100),
                              'invalid_handle')
        self.assertIn('handle-closed',inputs.events)
        self.assertNotIn('connect',inputs.events,'driver dispatch occurred after close')
        self.assertEqual(inputs.connections,[])

    async def test_x3close001_session_check_refuses_completed_retirement(self):
        api,inputs,resolver,_,_=self.fixture();e=self.deadline(2)
        from cortex_core.runtime.connections import ResolverConnection
        adapter=ResolverConnection(REF,'probe',INSTALL,'api',e)
        conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
        inputs.events.clear()
        entered,release=self.park_observation(inputs,lambda count:'session-observe' in inputs.events)
        pending=asyncio.create_task(adapter.check(conn,deadline=e))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await adapter.retire(conn,deadline=e)
            self.assertTrue(conn.closed)
        finally:release.set()
        await self.denied(pending,'invalid_handle')

    async def test_x3close001_waiting_observation_rechecks_fixed_supplier(self):
        api,inputs,resolver,_,_=self.fixture();e=self.deadline(2)
        h=await self.handle(resolver,'probe',deadline=e)
        entered,release=self.park_observation(inputs,lambda count:count==1)
        pending=asyncio.create_task(resolver.check(h,e))
        try:
            await asyncio.wait_for(entered.wait(),1)
            with patch.object(api,'_installed_inputs',return_value=Inputs(api)):
                release.set()
                await self.denied(pending,'authority_changed')
        finally:release.set()

    async def test_x3close001_waiting_observation_preserves_tighter_live_expiry(self):
        api,inputs,resolver,_,_=self.fixture();e=self.deadline(2)
        h=await self.handle(resolver,'probe',deadline=e)
        original=inputs.observe;entered=asyncio.Event();release=asyncio.Event();count=0
        async def observe(*args):
            nonlocal count
            result=await original(*args);count+=1
            if count==1:
                entered.set();await release.wait()
            else:result=replace(result,expires=self.deadline(.01))
            return result
        inputs.observe=observe
        pending=asyncio.create_task(resolver.check(h,e))
        try:
            await asyncio.wait_for(entered.wait(),1)
            await resolver.check(h,e)  # independently tighten the shared live record
            await asyncio.sleep(.02)
        finally:release.set()
        await self.denied(pending,'deadline_expired')
        await self.denied(resolver.check(h,e),'deadline_expired')


# Hale's seven reviewer methods are retained verbatim; use this module's fixtures.
import sys
fixtures=sys.modules[__name__]

class Residual(unittest.IsolatedAsyncioTestCase):
 api=fixtures.X3.api;fixture=fixtures.X3.fixture;deadline=fixtures.X3.deadline;handle=fixtures.X3.handle;denied=fixtures.X3.denied
 async def test_control_open_handle_and_current_supplier_pass(self):
  api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
  await resolver.check(h,self.deadline());h.close()
  await self.denied(resolver.check(h,self.deadline()),'invalid_handle')
 async def test_closed_during_current_check_must_refuse(self):
  api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
  original=inputs.observe;entered=asyncio.Event();release=asyncio.Event()
  async def waiting(*args):
   grant=await original(*args);entered.set();await release.wait();return grant
  inputs.observe=waiting
  task=asyncio.create_task(resolver.check(h,self.deadline()))
  await asyncio.wait_for(entered.wait(),.5);h.close();release.set()
  await self.denied(task,'invalid_handle')
 async def test_supplier_replaced_during_current_check_must_refuse(self):
  api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
  original=inputs.observe;entered=asyncio.Event();release=asyncio.Event()
  async def waiting(*args):
   grant=await original(*args);entered.set();await release.wait();return grant
  inputs.observe=waiting
  task=asyncio.create_task(resolver.check(h,self.deadline()))
  await asyncio.wait_for(entered.wait(),.5)
  api._installed_inputs.return_value=Inputs(api);release.set()
  await self.denied(task,'authority_changed')
 async def test_closed_during_callback_final_check_must_not_deliver_material(self):
  api,inputs,resolver,_,_=self.fixture();inputs.driver='asyncpg'
  from cortex_core.runtime.connections import ResolverConnection
  adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
  conn=await adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100)
  h=adapter._sessions[conn][1];calls=0;original=inputs.observe
  entered=asyncio.Event();release=asyncio.Event()
  async def waiting(*args):
   nonlocal calls
   calls+=1;grant=await original(*args)
   if calls==3:entered.set();await release.wait()
   return grant
  inputs.observe=waiting
  task=asyncio.create_task(inputs.password_callback())
  try:
   await asyncio.wait_for(entered.wait(),.5);h.close();release.set()
   await self.denied(task,'invalid_handle')
  finally:
   release.set();inputs.observe=original
   await adapter.retire(conn,deadline=self.deadline())
 async def test_control_cooperative_deadline_returns(self):
  api,inputs,resolver,_,_=self.fixture();inputs.delay=1
  await self.denied(self.handle(resolver,deadline=self.deadline(.02)),'deadline_expired')
 async def test_swallowed_timeout_must_return_before_supplier_release(self):
  api,inputs,resolver,_,_=self.fixture();h=await self.handle(resolver)
  original=inputs.observe;seen=asyncio.Event();release=asyncio.Event()
  async def hung(*args):
   try:await asyncio.Event().wait()
   except asyncio.CancelledError:seen.set();await release.wait()
   return await original(*args)
  inputs.observe=hung
  task=asyncio.create_task(resolver.check(h,self.deadline(.02)))
  try:
   done,_=await asyncio.wait({asyncio.create_task(seen.wait())},timeout=.15)
   self.assertTrue(done,'timeout cancellation did not arrive')
   await asyncio.sleep(.02)
   returned_by_bound=task.done()
  finally:
   release.set()
   try:await task
   except api.SecretRefusal:pass
  self.assertTrue(returned_by_bound,'X3 deadline elapsed but call remained owned by cancellation-swallowing supplier')
 async def test_external_connect_cancellation_must_not_publish_session(self):
  api,inputs,resolver,_,_=self.fixture();entered=asyncio.Event()
  original=inputs.connect
  async def swallow(*args):
   conn=await original(*args);entered.set()
   try:await asyncio.Event().wait()
   except asyncio.CancelledError:return conn
  inputs.connect=swallow
  from cortex_core.runtime.connections import ResolverConnection
  adapter=ResolverConnection(REF,'probe',INSTALL,'api',self.deadline())
  task=asyncio.create_task(adapter.connect(connect_timeout_seconds=.2,statement_timeout_ms=100))
  await asyncio.wait_for(entered.wait(),.5);task.cancel()
  try:
   try:conn=await task
   except asyncio.CancelledError:return
   await adapter.retire(conn,deadline=self.deadline())
   self.fail('X3 cancelled connect published a successful physical session')
  finally:
   for conn in inputs.connections:
    if not conn.closed:await conn.close()


if __name__=='__main__':unittest.main(verbosity=2)
