"""Current-clock/owner-scope source controls, never native or signing credit."""
import copy
from dataclasses import replace
import unittest

from aggregate_fixture import encode, sha256
import test_owner_startup_binding as support
from cortex_core.runtime import signed_member as port


class CurrentOriginalBounds(unittest.TestCase):
    def fixture(self):
        f=support.OwnerStartupBinding(methodName='runTest');f.setUp()
        self.addCleanup(f.doCleanups);f.prepare()
        return f

    def sync(self,f):
        f.pin()
        f.frame=replace(f.frame,owner_binding=(sha256(encode(f.policy)),11,27))
        f.request['release']['joint_index_ref'].update(bytes=len(f.raw),sha256=sha256(f.raw))
        scope=f.records[f.records[f.context['consent_ref']['path']]['scope_ref']['path']]
        f.records[scope['release_binding_ref']['path']]=copy.deepcopy(f.request['release'])

    def test_release_current_context_carries_owner_clock_interval_as_data(self):
        f=self.fixture();f.clock['uncertainty_seconds']=1
        with f.composition():
            startup,error=f.call(f.api()._installed_startup)
            self.assertIsNone(error,str(error))
            context=startup.current()
        self.assertEqual(getattr(context,'clock_interval',None),(f.clock['now_utc']-1,f.clock['now_utc']+1))

    def test_signed_manifest_expiry_checks_the_whole_admitted_interval(self):
        f=self.fixture();f.clock['uncertainty_seconds']=1
        f.value['expires_at']=f.clock['now_utc'];self.sync(f)
        with f.composition():
            _,error=f.call(lambda:port.request_member(f.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        f.assert_closed()

    def test_signed_manifest_not_before_checks_the_whole_admitted_interval(self):
        f=self.fixture();f.clock['uncertainty_seconds']=1
        f.value['not_before']=f.clock['now_utc'];self.sync(f)
        with f.composition():
            _,error=f.call(lambda:port.request_member(f.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        f.assert_closed()

    def test_current_policy_expiry_checks_the_whole_admitted_interval(self):
        f=self.fixture();f.clock['uncertainty_seconds']=1
        f.policy['validity']['expires_at']=f.clock['now_utc'];self.sync(f)
        with f.composition():
            _,error=f.call(lambda:port.request_member(f.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_replay')
        f.assert_closed()

    def test_same_owner_cannot_deliver_an_aggregate_outside_its_original_release_scope(self):
        f=self.fixture()
        f.request['release']['joint_index_ref']['sha256']='f'*64
        scope=f.records[f.records[f.context['consent_ref']['path']]['scope_ref']['path']]
        f.records[scope['release_binding_ref']['path']]=copy.deepcopy(f.request['release'])
        with f.composition():
            _,error=f.call(lambda:port.request_member(f.aggregate,'runtime.lock.json'))
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'aggregate_identity_mismatch')
        self.assertEqual(f.transports,[])
