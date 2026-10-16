"""RC3a frozen oracle: source interfaces only, never real grant authority.

Surface-absence RED is labelled; subsequent guard RED/mutations must fail BODY
assertions. All signed-member positives reuse RC2's non-authoritative test double.
"""
import asyncio
import copy
from dataclasses import FrozenInstanceError, replace
import importlib
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT/'tests/runtime'))
from test_admission import FIXTURE, AGGREGATE, MEMBER, encode
from non_authoritative_signed_member_double import NonAuthoritativeSignedMemberDouble
from cortex_core.runtime import signed_member
from cortex_core.runtime.config import AdmissionPlan

GAP = 'PG-A runtime-grants'
NAMES = ('write', 'read', 'search', 'status', 'enrollment')
ROUTES = ('C01-R102', 'C11-D1', 'C01-R022', 'C01-R030', None)


class FactoryOracle(unittest.IsolatedAsyncioTestCase):
    def modules(self):
        for name in ('factory', 'connections'):
            self.assertTrue((NEXT/'src/cortex_core/runtime'/f'{name}.py').is_file(),
                            f'SURFACE_ABSENCE: RC3a {name} interface missing')
        return (importlib.import_module('cortex_core.runtime.factory'),
                importlib.import_module('cortex_core.runtime.connections'))

    def materials(self):
        lock = copy.deepcopy(FIXTURE['lock'])
        cfg = copy.deepcopy(FIXTURE['mode_configs']['linux/arm64']['api'])
        from cortex_core.release.codec import sha256
        cfg['runtime_lock_sha256'] = sha256(encode(lock))
        return lock, cfg

    def snapshot(self, lock, cfg):
        return {'installation_id':cfg['installation_id'],
                'schema':copy.deepcopy(lock['current_schema']),
                'embedding':copy.deepcopy(cfg['embedding']['value']),
                'roles':copy.deepcopy(cfg['roles']),
                'grant_receipt':lock['owner_ports']['role_grants']['original_receipt']}

    def bindings(self, factory):
        async def never(*args, **kwargs):
            self.fail('RC3a invoked a handler/resolver/provider')
        return tuple(factory.HandlerBinding(n,r,never) for n,r in zip(NAMES,ROUTES))

    def describe(self, *, cfg_change=None, snapshot_change=None, bindings_change=None,
                 with_snapshot=True, provider=None, resolver=None):
        factory, _ = self.modules()
        lock, cfg = self.materials()
        if cfg_change:cfg_change(cfg)
        snapshot = self.snapshot(lock,cfg)
        if snapshot_change:snapshot_change(snapshot)
        bindings = self.bindings(factory)
        if bindings_change:bindings=bindings_change(bindings)
        double = NonAuthoritativeSignedMemberDouble(encode(lock))
        with double.installed(signed_member):
            member = signed_member.request_member(AGGREGATE,MEMBER)
        return factory.describe_factory(member,encode(cfg),aggregate_id=AGGREGATE,
            observed_target='linux/arm64',handlers=bindings,
            snapshot=snapshot if with_snapshot else None,provider=provider,resolver=resolver)

    def refused(self, reason, call, *, unit=None):
        with self.assertRaises(signed_member.Refusal) as caught:call()
        self.assertEqual(caught.exception.reason,reason)
        self.assertEqual(caught.exception.unit,unit)
        return caught.exception

    def test_nine_roles_are_immutable_data_not_connections(self):
        plan=self.describe()
        expected=set(self.materials()[1]['roles'])
        self.assertEqual({x.logical_name for x in plan.connections.roles},expected)
        self.assertEqual(len(plan.connections.roles),9)
        self.assertEqual(plan.authority_status,'UNBOUND')
        self.assertFalse(plan.effect_authority)
        self.assertEqual(plan.handler_names,NAMES)
        with self.assertRaises((AttributeError,FrozenInstanceError)):
            plan.connections.roles[0].sql_name='changed'

    def test_production_default_R01_gap_precedes_candidate_use(self):
        factory,_=self.modules()
        class Candidate:
            def __bytes__(self):raise AssertionError('candidate inspected')
        self.refused('required_binding_gap',lambda:factory.describe_factory(
            None,Candidate(),aggregate_id=AGGREGATE,observed_target='linux/arm64',
            handlers=()),unit=signed_member.MISSING_UNIT)

    def test_forged_RC2_plan_is_not_execution_authority(self):
        _,connections=self.modules()
        lock,cfg=self.materials()
        plan=AdmissionPlan('linux/arm64','api',(),'0'*64,(),effect_authority=True)
        self.refused('declaration_not_authority',lambda:connections.describe_connections(
            plan,lock,cfg,snapshot=self.snapshot(lock,cfg)))

    def test_RC2_role_collision_refuses_before_factory(self):
        self.refused('role_collision',lambda:self.describe(cfg_change=lambda c:
            c['roles']['probe']['binding']['value'].update(
                sql_name=c['roles']['api_request']['binding']['value']['sql_name'])))

    def test_wrong_schema_refuses(self):
        self.refused('schema_mismatch',lambda:self.describe(snapshot_change=lambda s:
            s['schema'].update(migration_count=19)))

    def test_wrong_installation_refuses(self):
        self.refused('installation_mismatch',lambda:self.describe(snapshot_change=lambda s:
            s.update(installation_id='00000000-0000-0000-0000-000000000001')))

    def test_wrong_model_refuses(self):
        self.refused('model_mismatch',lambda:self.describe(snapshot_change=lambda s:
            s['embedding'].update(model='different-model')))

    def test_wrong_grant_receipt_refuses(self):
        self.refused('grant_mismatch',lambda:self.describe(snapshot_change=lambda s:
            s.update(grant_receipt='00000000-0000-0000-0000-000000000001')))

    def test_bypass_service_roles_refuse(self):
        for name in ('api_request','probe','supervisor','observer','installer_enrollment'):
            for flag in ('superuser','bypassrls'):
                with self.subTest(role=name,flag=flag):
                    self.refused('bypass_role',lambda:self.describe(snapshot_change=lambda s:
                        s['roles'][name]['policy'].update({flag:True})))

    def test_inherited_or_extra_memberships_refuse(self):
        for change in ({'inherit':True},{'memberships':['kaidera-runtime-core-verifier']}):
            with self.subTest(change=change):
                self.refused('role_policy_mismatch',lambda:self.describe(snapshot_change=lambda s:
                    s['roles']['api_request']['policy'].update(change)))

    def test_role_catalogue_and_snapshot_are_closed(self):
        self.refused('role_catalogue_mismatch',lambda:self.describe(snapshot_change=lambda s:
            s['roles'].pop('observer')))
        self.refused('observation_shape',lambda:self.describe(snapshot_change=lambda s:
            s.update(BOUND=True)))

    def test_missing_handler_refuses(self):
        for index in range(5):
            with self.subTest(name=NAMES[index]):
                self.refused('missing_handler',lambda:self.describe(bindings_change=lambda b:
                    b[:index]+b[index+1:]))

    def test_noncallable_handler_refuses(self):
        self.refused('missing_handler',lambda:self.describe(bindings_change=lambda b:
            (replace(b[0],callback=None),)+b[1:]))

    def test_handler_collision_refuses(self):
        self.refused('handler_collision',lambda:self.describe(bindings_change=lambda b:b+(b[0],)))
        self.refused('handler_collision',lambda:self.describe(bindings_change=lambda b:
            (replace(b[0],route_id=b[1].route_id),)+b[1:]))

    def test_wrong_route_refuses(self):
        self.refused('handler_route_mismatch',lambda:self.describe(bindings_change=lambda b:
            (replace(b[0],route_id='C01-R001'),)+b[1:]))

    def test_role_views_never_offer_finite_or_owner_credentials(self):
        c=self.describe().connections
        for role in ('bootstrap','migrator','request_group','verifier_owner','installer_enrollment','supervisor','unknown'):
            with self.subTest(role=role):
                self.refused('role_not_in_process_view',lambda:c.for_role(role))
        self.assertEqual(c.for_role('probe').logical_name,'probe')

    async def test_probe_connection_matches_RC1_but_is_unbound(self):
        _,connections=self.modules()
        port=self.describe().connections.for_role('probe')
        self.assertTrue(inspect.iscoroutinefunction(port.connect))
        self.assertEqual(tuple(inspect.signature(port.connect).parameters),
                         ('connect_timeout_seconds','statement_timeout_ms'))
        with self.assertRaises(signed_member.Refusal) as caught:
            await port.connect(connect_timeout_seconds=.2,statement_timeout_ms=200)
        self.assertEqual((caught.exception.reason,caught.exception.unit),('required_binding_gap',GAP))
        from cortex_core.api.c11a import postgres_core_probe
        probe=postgres_core_probe(port,timeout_seconds=.2)
        self.assertIs(await probe(),False)
        await probe.aclose(deadline=asyncio.get_running_loop().time()+.2)
        self.assertIs(connections.RC1_PROBE,postgres_core_probe)

    async def test_factory_and_health_have_zero_provider_resolver_handler_effects(self):
        calls=[]
        async def canary(*args,**kwargs):calls.append('effect');raise AssertionError('effect')
        plan=self.describe(provider=canary,resolver=canary)
        with patch('socket.socket',side_effect=AssertionError('connection')):
            value=await plan.health()
        self.assertEqual(value,{'component':'cortex','status':'unavailable','readiness':'unavailable'})
        self.assertEqual(calls,[])
        self.refused('required_binding_gap',plan.require_services,unit=GAP)

    def test_matching_or_missing_observation_never_binds(self):
        for exists in (False,True):
            with self.subTest(snapshot=exists):
                plan=self.describe(with_snapshot=exists)
                self.refused('required_binding_gap',plan.require_services,unit=GAP)
                self.assertEqual(plan.enrollment_states,('enrollment_required','ready','unavailable'))
                self.assertIn('PG-B runtime-ports',plan.dependencies)
                self.assertIn('PG-D C04 connection adapters',plan.dependencies)
                self.assertFalse(plan.effect_authority)

    def test_no_public_binding_toggle_or_driver_factory(self):
        factory,connections=self.modules()
        for module in (factory,connections):
            for name in ('bind','connect','create_pool','resolve_key','BOUND'):
                self.assertFalse(hasattr(module,name),name)
        self.assertNotIn('dsn',repr(self.describe()).lower())
