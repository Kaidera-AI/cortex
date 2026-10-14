"""Frozen supplemental owner limits/provider boundary controls; keys only in tests."""
import base64
from dataclasses import replace
import hashlib
import importlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory


class Bounds(unittest.TestCase):
 def module(self):return importlib.import_module('cortex_core.release.trust')
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: exact refusal boundary')
  self.assertEqual(result.reason,reason,'BODY: exact limit/stage reason')

 def test_owner_lower_depth_is_enforced(self):
  module=self.module();auth=factory.test_authority();auth.policy['limits']['depth']=1
  self.refused('raw_limit',lambda:auth.verify(module))

 def test_owner_lower_manifest_bytes_before_crypto(self):
  module=self.module();auth=factory.test_authority();auth.policy['limits']['manifest_bytes']=len(auth.raw)-1
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('raw_limit',lambda:auth.verify(module))
  self.assertEqual(spy.call_count,0)

 def test_owner_lower_signature_bytes_before_crypto(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed();auth.policy['limits']['signature_bytes']=len(sig)-1
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('raw_limit',lambda:auth.verify(module,signature=sig))
  self.assertEqual(spy.call_count,0)

 def test_trusted_comment_bound_before_crypto(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed(trusted_comment='x'*513)
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('raw_limit',lambda:auth.verify(module,signature=sig))
  self.assertEqual(spy.call_count,0)

 def test_untrusted_comment_malformed_UTF8_before_crypto(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed().replace(b'test-only envelope',b'\xff',1)
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_signature_invalid',lambda:auth.verify(module,signature=sig))
  self.assertEqual(spy.call_count,0)

 def test_owner_policy_original_must_be_canonical(self):
  module=self.module();auth=factory.test_authority();ctx=auth.context(module);raw=ctx.policy_raw+b' '
  bad=replace(ctx,policy_raw=raw,policy_sha256=hashlib.sha256(raw).hexdigest())
  self.refused('required_binding_gap',lambda:module._verify_original(auth.raw,auth.signed(),bad))

 def test_provider_authentic_message_over_cap_is_false(self):
  from cortex_core.release.ed25519 import verify_ed25519
  auth=factory.test_authority();message=b'x'*1048577;signature=auth._signer.sign(message).signature
  self.assertFalse(verify_ed25519(auth.public,message,signature),'BODY: authentic oversized provider message admitted')

 def test_provider_unqualified_version_is_bounded(self):
  from cortex_core.release.ed25519 import verify_ed25519
  import nacl
  auth=factory.test_authority()
  with patch.object(nacl,'__version__','0.0.0'):
   with self.assertRaises(RuntimeError) as caught:verify_ed25519(auth.public,b'x',b'x'*64)
  self.assertEqual(str(caught.exception),'ed25519_provider_unqualified')

 def test_provider_missing_dependency_is_bounded(self):
  from cortex_core.release.ed25519 import verify_ed25519
  import builtins
  original=builtins.__import__
  def unavailable(name,*args,**kwargs):
   if name=='nacl':raise ImportError('SENTINEL_NATIVE_PATH')
   return original(name,*args,**kwargs)
  auth=factory.test_authority()
  with patch('builtins.__import__',side_effect=unavailable):
   with self.assertRaises(RuntimeError) as caught:verify_ed25519(auth.public,b'x',b'x'*64)
  self.assertEqual(str(caught.exception),'ed25519_provider_unavailable');self.assertIsNone(caught.exception.__cause__)

 def test_provider_import_does_not_load_native(self):
  code='import sys;sys.path.insert(0,'+repr(str(NEXT/'src'))+');import cortex_core.release.ed25519;assert not any(n=="nacl" or n.startswith("nacl.") for n in sys.modules)'
  completed=subprocess.run([sys.executable,'-I','-B','-c',code],capture_output=True,text=True)
  self.assertEqual(completed.returncode,0,'BODY: provider import loaded native dependency: '+completed.stderr)


def root_case(change):
 def test(self):
  module=self.module();auth=factory.test_authority();auth.root_material=change(auth.root_material)
  auth.policy['root'].update(public_key_bytes=len(auth.root_material),key_material_sha256=hashlib.sha256(auth.root_material).hexdigest())
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_root_mismatch',lambda:auth.verify(module))
  self.assertEqual(spy.call_count,0)
 return test


def pub_algorithm(raw):
 lines=raw.split(b'\n');packet=base64.b64decode(lines[1]);lines[1]=base64.b64encode(b'ED'+packet[2:]);return b'\n'.join(lines)

for label,change in {
 'empty':lambda r:b'',
 'over_cap':lambda r:b'x'*4097,
 'CRLF':lambda r:r.replace(b'\n',b'\r\n'),
 'no_final_LF':lambda r:r[:-1],
 'extra_line':lambda r:r+b'\n',
 'bad_base64':lambda r:r.replace(b'\nRW',b'\n!W',1),
 'wrong_public_algorithm':pub_algorithm,
}.items():setattr(Bounds,'test_root_'+label,root_case(change))

if __name__=='__main__':unittest.main(verbosity=2)
