"""Authentic hostile envelope controls that distinguish bounds from crypto failure."""
from dataclasses import replace
import hashlib
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory


class Guards(unittest.TestCase):
 def module(self):return importlib.import_module('cortex_core.release.trust')
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: boundary returned or leaked dependency exception')
  self.assertEqual(result.reason,reason,'BODY: exact owner/root/envelope guard')

 def test_authentic_root_over_cap_is_refused_before_crypto(self):
  module=self.module();auth=factory.test_authority();auth.root_material=auth.root_material.replace(b'TEST ONLY unrelated root',b'x'*4096)
  auth.policy['root'].update(public_key_bytes=len(auth.root_material),key_material_sha256=hashlib.sha256(auth.root_material).hexdigest())
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_root_mismatch',lambda:auth.verify(module))
  self.assertEqual(spy.call_count,0)

 def test_authentic_signature_over_cap_is_refused_before_crypto(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed(untrusted='x'*4096)
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('raw_limit',lambda:auth.verify(module,signature=sig))
  self.assertEqual(spy.call_count,0)

 def test_root_bytearray_is_refused_before_crypto(self):
  module=self.module();auth=factory.test_authority();ctx=replace(auth.context(module),public_root=bytearray(auth.root_material))
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_root_mismatch',lambda:module._verify_original(auth.raw,auth.signed(),ctx))
  self.assertEqual(spy.call_count,0)

 def test_owner_context_wrong_type_is_required_gap(self):
  module=self.module()
  self.refused('required_binding_gap',lambda:module._verify_original(b'x',b'x',object()))

 def test_production_policy_schema_is_required_even_with_private_provider(self):
  module=self.module();auth=factory.test_authority()
  # Test-only patch of the fixed-ID comparator permits reaching the still-strict
  # production schema. There is no root setter/test-mode in installed source.
  with patch.object(module,'_production_context',return_value=auth.context(module)),patch.object(module,'PRODUCTION_KEY_ID',auth.policy['root']['key_id']):
   self.refused('required_binding_gap',lambda:module.verify_manifest(auth.raw,auth.signed()))

 def test_policy_release_identity_is_independent(self):
  module=self.module();auth=factory.test_authority();auth.policy['release']['release_id']='v9.9.9'
  self.refused('aggregate_identity_mismatch',lambda:auth.verify(module))

 def test_policy_source_identity_is_independent(self):
  module=self.module();auth=factory.test_authority();auth.policy['source']['commit']='f'*40
  self.refused('aggregate_identity_mismatch',lambda:auth.verify(module))

 def test_owner_maximum_release_age_is_enforced(self):
  module=self.module();auth=factory.test_authority();auth.policy['validity']['maximum_release_age_seconds']=1
  self.refused('aggregate_replay',lambda:auth.verify(module))


def untrusted_control(where,byte):
 def test(self):
  module=self.module();auth=factory.test_authority()
  if where=='root':
   auth.root_material=auth.root_material.replace(b'TEST ONLY unrelated root',b'test'+byte)
   auth.policy['root'].update(public_key_bytes=len(auth.root_material),key_material_sha256=hashlib.sha256(auth.root_material).hexdigest())
   signature=auth.signed();reason='aggregate_root_mismatch'
  else:
   signature=auth.signed().replace(b'test-only envelope',b'test'+byte);reason='aggregate_signature_invalid'
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused(reason,lambda:auth.verify(module,signature=signature))
  self.assertEqual(spy.call_count,0)
 return test

for where in ('root','signature'):
 for label,byte in [('CR',b'\r'),('NUL',b'\0')]:
  setattr(Guards,'test_'+where+'_untrusted_'+label+'_is_structurally_refused',untrusted_control(where,byte))

if __name__=='__main__':unittest.main(verbosity=2)
