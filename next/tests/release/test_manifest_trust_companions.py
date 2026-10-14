"""Additional original body controls; no frozen deciding assertion is edited."""
import base64
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory


def trust():return importlib.import_module('cortex_core.release.trust')


class Companions(unittest.TestCase):
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: expected typed refusal instead of return/raw dependency exception')
  self.assertEqual(result.reason,reason,'BODY: exact stage reason')
  return result

 def test_owner_provider_fault_is_sanitized(self):
  module=trust()
  with patch.object(module,'_production_context',side_effect=RuntimeError('SENTINEL_OWNER_SECRET_PATH')):
   error=self.refused('member_authority_unavailable',lambda:module.verify_manifest(b'x',b'x'))
  self.assertNotIn('SENTINEL',str(error));self.assertIsNone(error.__cause__)

 def test_crafted_signature_type_never_invokes_equality(self):
  module=trust();auth=factory.test_authority()
  class Crafted:
   calls=0
   def __eq__(self,other):self.calls+=1;return True
  bad=Crafted()
  self.refused('raw_limit',lambda:auth.verify(module,signature=bad))
  self.assertEqual(bad.calls,0,'BODY: crafted equality ran before exact-byte guard')

 def test_noncanonical_base64_padbits_before_crypto(self):
  module=trust();auth=factory.test_authority();lines=auth.signed().split(b'\n');alphabet=b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  chars=bytearray(lines[1]);chars[-2]=alphabet[alphabet.index(chars[-2])+1];lines[1]=bytes(chars)
  self.assertEqual(base64.b64decode(lines[1]),base64.b64decode(auth.signed().split(b'\n')[1]))
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_signature_invalid',lambda:auth.verify(module,signature=b'\n'.join(lines)))
  self.assertEqual(spy.call_count,0,'BODY: noncanonical encoding reached crypto')

 def test_signed_duplicate_json_key_refuses(self):
  module=trust();auth=factory.test_authority();raw=auth.raw.replace(b'{',b'{"format_version":1,',1)
  self.refused('aggregate_shape',lambda:auth.verify(module,raw=raw,signature=auth.signed(raw=raw)))

 def test_signed_depth_over32_refuses(self):
  module=trust();auth=factory.test_authority();value=json.loads(auth.raw);nested=0
  for _ in range(34):nested=[nested]
  value['extra']=nested;raw=factory.canonical(value)
  self.refused('raw_limit',lambda:auth.verify(module,raw=raw,signature=auth.signed(raw=raw)))

 def test_owner_policy_original_hash_is_independent(self):
  module=trust();auth=factory.test_authority();ctx=auth.context(module)
  from dataclasses import replace
  bad=replace(ctx,policy_sha256='f'*64)
  self.refused('required_binding_gap',lambda:module._verify_original(auth.raw,auth.signed(),bad))

 def test_root_decoded_id_matches_independent_owner(self):
  module=trust();auth=factory.test_authority();auth.policy['root']['key_id']='0'*16
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.refused('aggregate_root_mismatch',lambda:auth.verify(module))
  self.assertEqual(spy.call_count,0)

 def test_owner_placement_positive_is_signature_data_only(self):
  module=trust();auth=factory.test_authority();auth.placement='owner';result=auth.verify(module)
  self.assertEqual(result.manifest_raw,auth.raw);self.assertFalse(result.effect_authority)

 def test_reverse_Cortex_subject_is_refused_by_unchanged_KOS_consumer(self):
  # Existing KOS source is unchanged. This TEST-only cryptography-API adapter
  # performs actual verification through the ONE real cryptography boundary; no second
  # provider, root binder or reimplementation of KOS's parser is installed.
  from cortex_core.release.ed25519 import verify_ed25519
  class InvalidSignature(Exception):pass
  class Public:
   def __init__(self,key):self.key=key
   @classmethod
   def from_public_bytes(cls,key):return cls(key)
   def verify(self,signature,message):
    if not verify_ed25519(self.key,message,signature):raise InvalidSignature()
  modules={name:ModuleType(name) for name in ['cryptography','cryptography.exceptions','cryptography.hazmat','cryptography.hazmat.primitives','cryptography.hazmat.primitives.asymmetric','cryptography.hazmat.primitives.asymmetric.ed25519']}
  modules['cryptography.exceptions'].InvalidSignature=InvalidSignature
  modules['cryptography.hazmat.primitives.asymmetric.ed25519'].Ed25519PublicKey=Public
  with patch.dict(sys.modules,modules):
   kos=importlib.import_module('r01u3_support.kos_snapshot.trust')
  auth=factory.test_authority();signature=auth.signed()
  # Control authenticates both signatures, and tampering must really fail.
  checked=kos.verify_minisign(auth.raw,signature.decode(),auth.root_material.decode())
  self.assertTrue(checked['prehashed']);self.assertEqual(checked['key_id'],auth.key_id[::-1].hex().upper())
  with self.assertRaises(kos.Refusal):kos.verify_minisign(auth.raw+b'x',signature.decode(),auth.root_material.decode())
  with tempfile.TemporaryDirectory(prefix='r01u3-kos-scope-',dir='/Users/amadmalik/DevVault/helix/output/KOS/ren-kos-r01u3b-test-env-20261014') as path:
   root=Path(path);(root/'release.json').write_bytes(auth.raw);(root/'release.json.minisig').write_bytes(signature)
   with self.assertRaises(kos.Refusal) as caught:
    kos.verify_package(root,public_key=auth.root_material.decode(),platform='macos-arm64',image_platform='linux/arm64')
  self.assertEqual(caught.exception.code,'release_schema_unsupported','BODY: unchanged KOS consumer admitted Cortex schema')

if __name__=='__main__':unittest.main(verbosity=2)
