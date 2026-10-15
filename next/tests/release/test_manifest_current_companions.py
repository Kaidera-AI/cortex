"""Authenticated pipeline companions for the frozen current-state oracle."""
from dataclasses import replace
import copy
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory

class CurrentPipeline(unittest.TestCase):
 def module(self):return importlib.import_module('cortex_core.release.trust')
 def refuse(self,reason,fn):
  caught=None
  try:fn()
  except Exception as error:caught=error
  self.assertIsInstance(caught,Refusal,'BODY: current-state boundary leaked or admitted')
  self.assertEqual(caught.reason,reason,'BODY: exact current-state disposition')
 def fresh(self,empty=False):
  a=factory.test_authority()
  if empty:a.policy['installed_baseline']=dict(state='empty',receipt_sha256='e'*64,fence=1,policy_generation=1)
  a.generation=3;a.fence=5;a.policy['owner_binding']['policy_generation']=3
  a.current_state=dict(installed_baseline=copy.deepcopy(a.policy['installed_baseline']),policy_generation=3,fence=5)
  return a
 def test_installed_older_attestation_authenticates(self):
  m=self.module();a=self.fresh();result=a.verify(m)
  self.assertEqual(result.manifest_raw,a.raw)
  self.assertFalse(result.native_qualified or result.effect_authority or result.members_admitted)
 def test_empty_older_attestation_authenticates(self):
  m=self.module();a=self.fresh(True);self.assertEqual(a.verify(m).manifest_raw,a.raw)
 def test_missing_current_state_precedes_candidate_and_crypto(self):
  m=self.module();a=self.fresh();ctx=replace(a.context(m),current_state=None)
  with patch.object(m,'verify_ed25519',wraps=m.verify_ed25519) as spy:
   self.refuse('required_binding_gap',lambda:m._verify_original(object(),None,ctx))
  self.assertEqual(spy.call_count,0,'BODY: missing mandatory current state accessed candidate crypto')
 def test_boolean_current_state_precedes_crypto(self):
  m=self.module();a=self.fresh();a.current_state['fence']=True
  with patch.object(m,'verify_ed25519',wraps=m.verify_ed25519) as spy:
   self.refuse('required_binding_gap',lambda:a.verify(m))
  self.assertEqual(spy.call_count,0)
 def test_attestation_mismatch_after_authentic_pair(self):
  m=self.module();a=self.fresh();a.policy['installed_baseline']['receipt_sha256']='f'*64
  with patch.object(m,'verify_ed25519',wraps=m.verify_ed25519) as spy:
   self.refuse('aggregate_replay',lambda:a.verify(m))
  self.assertEqual(spy.call_count,2,'BODY: sole attestation fault did not follow actual signature pair')
 def test_observed_current_fence_drift_after_authentic_pair(self):
  m=self.module();a=self.fresh();a.fence=6
  with patch.object(m,'verify_ed25519',wraps=m.verify_ed25519) as spy:
   self.refuse('aggregate_replay',lambda:a.verify(m))
  self.assertEqual(spy.call_count,2)
 def test_no_public_production_current_state_default(self):
  m=self.module();self.assertIs(m._production_context,m._unbound)
  with patch.object(m,'verify_ed25519',wraps=m.verify_ed25519) as spy:
   self.refuse('required_binding_gap',lambda:m.verify_manifest(object(),None))
  self.assertEqual(spy.call_count,0)
if __name__=='__main__':unittest.main(verbosity=2)
