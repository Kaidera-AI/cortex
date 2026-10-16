"""Extra source ordering controls; no native/host/effect qualification."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_owner_startup_binding as support
from cortex_core.install import owner
from cortex_core.runtime import signed_member as port


class OwnerStartupOrder(unittest.TestCase):
    def fixture(self):
        value=support.OwnerStartupBinding(methodName='runTest')
        value.setUp();self.addCleanup(value.doCleanups);value.prepare()
        return value

    def test_cold_default_refuses_before_package_original_reads(self):
        code='''# CORTEX_SOURCE_COLD_BOUNDARY
import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from cortex_core.runtime import signed_member
reads=[]
def denied(*args,**kwargs):
 reads.append(str(args[0]));raise AssertionError('unexpected package-original read')
with patch.object(Path,'read_bytes',denied):
 try:signed_member.request_member(object(),object())
 except signed_member.Refusal as e:result=dict(reason=e.reason,unit=e.unit,reads=reads)
print(json.dumps(result))
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(support.NEXT/'src')],
            capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        observed=json.loads(result.stdout)
        self.assertEqual(observed,dict(reason='required_binding_gap',unit=port.MISSING_UNIT,reads=[]))

    def test_exact_signature_refusal_precedes_deferred_current_custody_drift(self):
        f=self.fixture();f.signature=f.signature.replace(b'\n',b'\r\n')
        request=f.inputs().originals[f.context['request_ref']['path']]
        def drift():raise owner._CurrentObservationRefusal(f.inputs(),owner.OwnerRefusal('custody_changed'))
        f.owner_hook=drift
        with f.composition():
            try:owner.propose_effect(request);error=None
            except owner.OwnerRefusal as error:observed=error
        self.assertEqual(observed.detail,'r01_other_refusal')
        self.assertEqual(observed.r01_reason,'aggregate_signature_invalid')
        self.assertEqual(f.transports,[f.aggregate])
        f.assert_closed()

    def test_valid_R01_finishes_before_deferred_current_drift_is_reported(self):
        f=self.fixture();request=f.inputs().originals[f.context['request_ref']['path']]
        def drift():raise owner._CurrentObservationRefusal(f.inputs(),owner.OwnerRefusal('custody_changed'))
        f.owner_hook=drift
        with f.composition():
            try:owner.propose_effect(request);error=None
            except owner.OwnerRefusal as error:observed=error
        self.assertEqual(observed.detail,'custody_changed')
        self.assertEqual((observed.result,observed.reason_code),('HOLD','state_drift'))
        self.assertEqual(f.transports,[f.aggregate]*2)
        f.assert_closed()

    def test_owner_frame_refresh_cannot_renew_the_original_audit_deadline(self):
        f=self.fixture();f.context['host_lease']['expires_at']+=100
        with f.composition():
            pair,error=f.call(f.api()._owner_originals)
            self.assertIsNone(error,str(error))
            current,_=pair
            f.clock['now_utc']+=31;f.clock['monotonic_ns']+=31_000_000_000
            start=f.records[f.context['clock_ref']['path']]
            start['now_utc']+=31;start['monotonic_ns']+=31_000_000_000
            _,error=f.call(current)
        self.assertIsNotNone(error)
        self.assertEqual(error.reason,'member_authority_unavailable')
        self.assertEqual(f.transports,[])
