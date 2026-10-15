"""Frozen independent owner-type/algorithm and exception-sanitization controls."""
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory


class PolicyGuards(unittest.TestCase):
 def module(self):return importlib.import_module('cortex_core.release.trust')
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: original owner boundary returned or leaked raw exception')
  self.assertEqual(result.reason,reason,'BODY: exact independent policy refusal')
  return result

 def test_owner_algorithm_legacy_despite_actual_ED_signature(self):
  module=self.module();auth=factory.test_authority();auth.policy['root']['signature_algorithm']='Ed'
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_algorithm_unsupported',lambda:auth.verify(module))
  self.assertEqual(spy.call_count,0)

 def test_root_id_string_subclass_is_refused(self):
  module=self.module();auth=factory.test_authority()
  class Subclass(str):pass
  auth.policy['root']['key_id']=Subclass(auth.policy['root']['key_id'])
  self.refused('aggregate_root_mismatch',lambda:auth.verify(module))

 def test_observed_boolean_generation_is_refused(self):
  module=self.module();auth=factory.test_authority();auth.generation=True
  self.refused('aggregate_replay',lambda:auth.verify(module))

 def test_owner_policy_raw_bytearray_is_required_gap(self):
  module=self.module();auth=factory.test_authority();ctx=replace(auth.context(module),policy_raw=bytearray(auth.context(module).policy_raw))
  self.refused('required_binding_gap',lambda:module._verify_original(auth.raw,auth.signed(),ctx))

 def test_owner_policy_canonical_failure_is_required_gap(self):
  module=self.module();auth=factory.test_authority();auth.policy['validity']['maximum_release_age_seconds']=1.0
  self.refused('required_binding_gap',lambda:auth.verify(module))

 def test_unexpected_provider_failure_is_sanitized(self):
  module=self.module();auth=factory.test_authority()
  with patch.object(module,'verify_ed25519',side_effect=RuntimeError('SENTINEL_NATIVE_SECRET')):
   error=self.refused('member_authority_unavailable',lambda:auth.verify(module))
  self.assertNotIn('SENTINEL',str(error));self.assertIsNone(error.__cause__)
  self.assertTrue(error.__suppress_context__,'BODY: dependency traceback was not suppressed')

 def test_depth_hard_cap_even_if_private_owner_proposes_larger(self):
  module=self.module();auth=factory.test_authority();auth.policy['limits']['depth']=64
  value=json.loads(auth.raw);nested=0
  for _ in range(34):nested=[nested]
  value['extra']=nested;raw=factory.canonical(value)
  self.refused('raw_limit',lambda:auth.verify(module,raw=raw,signature=auth.signed(raw=raw)))

if __name__=='__main__':unittest.main(verbosity=2)
