"""Frozen baseline-versus-current controls; no crypto, keys or effect fixtures."""
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

NEXT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'src'))
from cortex_core.runtime.signed_member import Refusal

def subjects(empty=False):
 policy=json.loads((NEXT/'tests/release/r01u3_support/production-policy.shape.json').read_bytes())
 value=json.loads((NEXT/'tests/release/r01u3_support/aggregate.canonical.json').read_bytes())
 if empty:policy['installed_baseline']=dict(state='empty',receipt_sha256='e'*64,fence=1,policy_generation=1)
 policy['owner_binding']['policy_generation']=2
 baseline=copy.deepcopy(policy['installed_baseline'])
 context=SimpleNamespace(now=1500,generation=2,fence=3,current_state=dict(installed_baseline=baseline,policy_generation=2,fence=3))
 return policy,value,context

class Alignment(unittest.TestCase):
 def check(self,p,v,c):return importlib.import_module('cortex_core.release.trust')._current(p,v,c)
 def refused(self,reason,p,v,c):
  caught=None
  try:self.check(p,v,c)
  except Exception as error:caught=error
  self.assertIsInstance(caught,Refusal,'BODY: typed refusal required, not error or success')
  self.assertEqual(caught.reason,reason,'BODY: exact missing/drift disposition')
 def accepted(self,p,v,c):
  try:result=self.check(p,v,c)
  except Exception as error:self.fail('BODY: authentic older baseline rejected by '+type(error).__name__)
  self.assertIsNone(result,'BODY: current guard returned non-data result')
 def test_installed_attestation_older_than_current(self):
  p,v,c=subjects();self.accepted(p,v,c)
 def test_empty_attestation_older_than_current(self):
  p,v,c=subjects(True);self.accepted(p,v,c)
 def test_installed_zero_historical_fence_can_have_positive_current(self):
  p,v,c=subjects();p['installed_baseline']['fence']=0;c.current_state['installed_baseline']['fence']=0
  self.accepted(p,v,c)
 def test_missing_state_has_no_default(self):
  p,v,c=subjects();del c.current_state;self.refused('required_binding_gap',p,v,c)
 def test_none_state_has_no_default(self):
  p,v,c=subjects();c.current_state=None;self.refused('required_binding_gap',p,v,c)
 def test_missing_attestation(self):
  p,v,c=subjects();c.current_state.pop('installed_baseline');self.refused('required_binding_gap',p,v,c)
 def test_none_attestation_is_not_empty(self):
  p,v,c=subjects();c.current_state['installed_baseline']=None;self.refused('required_binding_gap',p,v,c)
 def test_missing_current_generation(self):
  p,v,c=subjects();c.current_state.pop('policy_generation');self.refused('required_binding_gap',p,v,c)
 def test_missing_current_fence(self):
  p,v,c=subjects();c.current_state.pop('fence');self.refused('required_binding_gap',p,v,c)
 def test_current_extra_field_refuses(self):
  p,v,c=subjects();c.current_state['default']=True;self.refused('required_binding_gap',p,v,c)
 def test_current_generation_drift(self):
  p,v,c=subjects();c.generation=3;self.refused('aggregate_replay',p,v,c)
 def test_current_fence_drift(self):
  p,v,c=subjects();c.fence=4;self.refused('aggregate_replay',p,v,c)
 def test_policy_generation_drift(self):
  p,v,c=subjects();p['owner_binding']['policy_generation']=3;self.refused('aggregate_replay',p,v,c)

def invalid(label,field,value):
 def test(self):
  p,v,c=subjects();c.current_state[field]=value;self.refused('required_binding_gap',p,v,c)
 return test
for field in ('policy_generation','fence'):
 for label,value in [('bool',True),('zero',0),('negative',-1),('float',2.0),('over_cap',2**63),('none',None)]:
  setattr(Alignment,'test_invalid_'+field+'_'+label,invalid(label,field,value))
def drift(field,value,empty):
 def test(self):
  p,v,c=subjects(empty);p['installed_baseline'][field]=value;self.refused('aggregate_replay',p,v,c)
 return test
for empty in (False,True):
 for field,value in [('receipt_sha256','f'*64),('policy_generation',2),('fence',2)]:
  setattr(Alignment,'test_attestation_drift_'+str(empty)+'_'+field,drift(field,value,empty))

if __name__=='__main__':unittest.main(verbosity=2)

