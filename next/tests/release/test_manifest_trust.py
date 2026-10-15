"""Frozen U3-B original-byte/owner-root oracle. Keys are generated only in tests.

Source-only signature receipts never admit OCI/member/native/install/effects.
All fixture members/owner records are inert; no release key or key file.
"""
import ast
import base64
import copy
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


def product():
 try:return importlib.import_module('cortex_core.release.trust')
 except ModuleNotFoundError as error:
  if error.name!='cortex_core.release.trust':raise
  return None


def mutate_packet(signature,change):
 lines=signature.decode().splitlines();raw=base64.b64decode(lines[1]);lines[1]=base64.b64encode(change(raw)).decode()
 return ('\n'.join(lines)+'\n').encode()


class Trust(unittest.TestCase):
 def module(self):
  module=product();self.assertIsNotNone(module,'SURFACE_ABSENCE: sole R01 trust body')
  self.assertTrue(callable(getattr(module,'verify_manifest',None)),'SURFACE_ABSENCE: verify_manifest')
  self.assertTrue(callable(getattr(module,'_verify_original',None)),'SURFACE_ABSENCE: private owner-context body')
  return module

 def check_refusal(self,expected,fn,label):
  reason=None
  try:fn()
  except Refusal as error:reason=error.reason
  self.assertEqual(reason,expected,'BODY: '+label+' expected exact refusal')

 def test_original_actual_two_signature_positive(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed();result=auth.verify(module,signature=sig)
  self.assertEqual(result.manifest_raw,auth.raw)
  self.assertEqual(result.manifest_sha256,hashlib.sha256(auth.raw).hexdigest())
  self.assertEqual(result.signature_sha256,hashlib.sha256(sig).hexdigest())
  self.assertEqual(result.signing_root_sha256,hashlib.sha256(auth.root_material).hexdigest())
  self.assertEqual(result.signing_key_id,auth.key_id[::-1].hex().upper())
  self.assertFalse(result.native_qualified);self.assertFalse(result.effect_authority);self.assertFalse(result.members_admitted)
  with self.assertRaises((AttributeError,TypeError)):result.manifest_sha256='forged'

 def test_production_default_unbound_before_any_candidate_or_provider(self):
  module=self.module()
  class NoRead:
   def __len__(self):raise AssertionError('candidate read')
  with patch('builtins.open',side_effect=AssertionError('IO')),patch('socket.socket',side_effect=AssertionError('socket')),patch('subprocess.Popen',side_effect=AssertionError('process')):
   with self.assertRaises(Refusal) as caught:module.verify_manifest(NoRead(),NoRead())
  self.assertEqual(caught.exception.reason,'required_binding_gap')
  self.assertEqual(caught.exception.unit,'R01 unit 3: signed Core/API aggregate and member admission')
  self.assertIs(module._production_context,module._unbound)

 def test_no_public_binder_root_mode_or_signer(self):
  module=self.module()
  for name in ('bind','set_root','test_root','test_mode','test_authority','SigningKey'):
   self.assertFalse(hasattr(module,name),'BODY: public root/signing bypass '+name)
  for path in (NEXT/'src/cortex_core/release/trust.py',NEXT/'src/cortex_core/release/ed25519.py'):
   tree=ast.parse(path.read_text())
   self.assertFalse(any(isinstance(n,ast.Constant) and isinstance(n.value,str) and 'r01u3_support' in n.value for n in ast.walk(tree)), 'BODY: product imports test factory')
   self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr=='generate' for n in ast.walk(tree)), 'BODY: product key generation')

 def test_test_root_is_refused_by_production_policy_path(self):
  module=self.module();auth=factory.test_authority()
  with patch.object(module,'_production_context',return_value=auth.context(module)):
   self.check_refusal('aggregate_root_mismatch',lambda:module.verify_manifest(auth.raw,auth.signed()),'unrelated test root in production loader')

 def test_provider_called_on_blake2b_original_then_exact_signature_comment(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed();lines=sig.decode().splitlines();packet=base64.b64decode(lines[1]);first=packet[10:];trusted=lines[2][len('trusted comment: '):];second=base64.b64decode(lines[3])
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   auth.verify(module,signature=sig)
  self.assertEqual(spy.call_count,2)
  self.assertEqual(spy.call_args_list[0].args,(auth.public,hashlib.blake2b(auth.raw,digest_size=64).digest(),first))
  self.assertEqual(spy.call_args_list[1].args,(auth.public,first+trusted.encode(),second))

 def test_untrusted_comment_is_not_authority(self):
  module=self.module();auth=factory.test_authority();sig=auth.signed();lines=sig.split(b'\n');lines[0]=b'untrusted comment: arbitrary KOS-looking untrusted text'
  result=auth.verify(module,signature=b'\n'.join(lines));self.assertEqual(result.manifest_raw,auth.raw)

 def test_signature_authenticated_before_manifest_parser(self):
  module=self.module();auth=factory.test_authority();sig=mutate_packet(auth.signed(),lambda p:p[:10]+bytes([p[10]^1])+p[11:])
  with patch.object(module,'decode_json',side_effect=AssertionError('candidate parsed before signature')):
   self.check_refusal('aggregate_signature_invalid',lambda:auth.verify(module,signature=sig),'first signature before decode')

 def test_member_content_is_not_read_or_admitted_in_B(self):
  module=self.module();auth=factory.test_authority()
  with patch('builtins.open',side_effect=AssertionError('physical member read')):
   # Material and fixed schema were preloaded at module import. No candidate member IO.
   result=auth.verify(module)
  self.assertFalse(result.members_admitted);self.assertFalse(result.native_qualified)

 def test_unexpected_crypto_dependency_is_sanitized(self):
  module=self.module();auth=factory.test_authority()
  with patch.object(module,'verify_ed25519',side_effect=RuntimeError('SENTINEL_SECRET_NATIVE_PATH')):
   with self.assertRaises(Refusal) as caught:auth.verify(module)
  self.assertEqual(caught.exception.reason,'member_authority_unavailable')
  self.assertNotIn('SENTINEL',str(caught.exception));self.assertIsNone(caught.exception.__cause__)

 def test_cached_factory_refuses_product_caller_before_key_generation(self):
  with patch.object(factory.SigningKey,'generate',side_effect=AssertionError('keygen reached')):
   code=compile('factory.test_authority()',str(NEXT/'src/cortex_core/_bad_test_import.py'),'exec')
   with self.assertRaises(ImportError):exec(code,{'factory':factory})

 def test_uncached_factory_refuses_product_import(self):
  name='r01u3_support.factory';saved=sys.modules.pop(name)
  try:
   code=compile('import importlib; importlib.import_module("r01u3_support.factory")',str(NEXT/'src/cortex_core/_bad_test_import.py'),'exec')
   with self.assertRaises(ImportError):exec(code,{})
  finally:sys.modules[name]=saved


def case(label,reason,change,*,zero_crypto=False):
 def test(self):
  module=self.module();auth=factory.test_authority();raw=auth.raw;sig=auth.signed()
  raw,sig=change(auth,raw,sig)
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as spy:
   self.check_refusal(reason,lambda:auth.verify(module,raw=raw,signature=sig),label)
  if zero_crypto:self.assertEqual(spy.call_count,0,'BODY: '+label+' reached crypto before refusal')
 return test


def owner_change(field,value):
 def change(a,r,s):a.policy[field]=value;return r,s
 return change


def root_change(field,value):
 def change(a,r,s):a.policy['root'][field]=value;return r,s
 return change


def signed_mutation(change):
 def changed(a,r,s):
  value=json.loads(r);change(value);raw=factory.canonical(value)
  # Independent expected manifest is updated only for shape tests. It grants no native data.
  a.policy['expected_aggregate_sha256']=hashlib.sha256(raw).hexdigest()
  return raw,a.signed(raw)
 return changed


def second_signature_bad(a,r,s):
 lines=s.decode().splitlines();raw=base64.b64decode(lines[3]);lines[3]=base64.b64encode(bytes([raw[0]^1])+raw[1:]).decode();return r,('\n'.join(lines)+'\n').encode()


def replaced_root_with_valid_other_signature(a,r,s):
 other=factory.test_authority();other.key_id=a.key_id
 a.root_material=(b'untrusted comment: TEST ONLY unrelated root\n'+base64.b64encode(b'Ed'+a.key_id+other.public)+b'\n')
 return r,other.signed(raw=r,signature_key_id=a.key_id)


CASES=[
 ('wrong_signature_key_id','aggregate_root_mismatch',lambda a,r,s:(r,a.signed(signature_key_id=b'\0'*8)),True),
 ('wrong_root_pin','aggregate_root_mismatch',root_change('key_material_sha256','f'*64),True),
 ('wrong_root_declared_length','aggregate_root_mismatch',lambda a,r,s:(a.policy['root'].update(public_key_bytes=len(a.root_material)+1) or r,s),True),
 ('wrong_root_bytes_valid_other_sig','aggregate_root_mismatch',replaced_root_with_valid_other_signature,True),
 ('signature_other_original_bytes','aggregate_signature_invalid',lambda a,r,s:(r,a.signed(raw=r+b' ')),False),
 ('altered_first_signature','aggregate_signature_invalid',lambda a,r,s:(r,mutate_packet(s,lambda p:p[:10]+bytes([p[10]^1])+p[11:])),False),
 ('altered_global_signature','aggregate_signature_invalid',second_signature_bad,False),
 ('altered_trusted_comment','aggregate_signature_invalid',lambda a,r,s:(r,s.replace(b'purpose=cortex/core-api/test',b'purpose=cortex/core-api/prod')),False),
 ('legacy_Ed','aggregate_algorithm_unsupported',lambda a,r,s:(r,a.signed(algorithm=b'Ed')),True),
 ('unknown_algorithm','aggregate_algorithm_unsupported',lambda a,r,s:(r,a.signed(algorithm=b'XX')),True),
 ('unsigned_none','aggregate_unsigned',lambda a,r,s:(r,None),True),
 ('unsigned_empty','aggregate_unsigned',lambda a,r,s:(r,b''),True),
 ('truncated_envelope','aggregate_signature_invalid',lambda a,r,s:(r,s[:-4]),True),
 ('signature_oversized','raw_limit',lambda a,r,s:(r,b'x'*4097),True),
 ('manifest_oversized','raw_limit',lambda a,r,s:(b'x'*1048577,s),True),
 ('signature_wrong_type','raw_limit',lambda a,r,s:(r,s.decode()),True),
 ('manifest_wrong_type','raw_limit',lambda a,r,s:(json.loads(r),s),True),
 ('base64_invalid','aggregate_signature_invalid',lambda a,r,s:(r,s.replace(b'\nRU',b'\n!U',1)),True),
 ('signature_extra_line','aggregate_signature_invalid',lambda a,r,s:(r,s+b'\n'),True),
 ('signature_CRLF','aggregate_signature_invalid',lambda a,r,s:(r,s.replace(b'\n',b'\r\n')),True),
 ('signature_no_final_LF','aggregate_signature_invalid',lambda a,r,s:(r,s[:-1]),True),
 ('KOS_purpose_in_Cortex','aggregate_scope_mismatch',lambda a,r,s:(r,a.signed(trusted_comment='kos/build-a/test')),False),
 ('comment_alias','aggregate_scope_mismatch',lambda a,r,s:(r,a.signed(trusted_comment=factory.comment(r,a.value).replace('cortex.release-aggregate','Cortex.release-aggregate'))),False),
 ('policy_replay_floor','aggregate_replay',owner_change('sequence_floor',2),False),
 ('policy_expired','aggregate_replay',lambda a,r,s:(setattr(a,'now',2001) or r,s),False),
 ('policy_generation_changed','aggregate_replay',lambda a,r,s:(setattr(a,'generation',2) or r,s),False),
 ('policy_fence_changed','aggregate_replay',lambda a,r,s:(setattr(a,'fence',2) or r,s),False),
 ('policy_now_unavailable','aggregate_replay',lambda a,r,s:(setattr(a,'now',None) or r,s),False),
 ('current_manifest_digest_differs','aggregate_identity_mismatch',owner_change('expected_aggregate_sha256','f'*64),False),
 ('wrong_owner_purpose','aggregate_scope_mismatch',owner_change('purpose','cortex/core-api/production'),False),
 ('root_key_use_denied','aggregate_scope_mismatch',root_change('key_use_permission','kos-build'),False),
 ('root_allowed_purpose_denied','aggregate_scope_mismatch',root_change('allowed_purposes',['cortex/core-api/production']),False),
 ('placement_core','aggregate_scope_mismatch',lambda a,r,s:(setattr(a,'placement','core') or r,s),True),
 ('placement_api','aggregate_scope_mismatch',lambda a,r,s:(setattr(a,'placement','api') or r,s),True),
 ('signed_unknown_field','aggregate_shape',signed_mutation(lambda v:v.update(root_from_artifact='ignored')),False),
 ('signed_KOS_schema','aggregate_shape',signed_mutation(lambda v:v.update(schema='kaidera-os.release.v1')),False),
 ('signed_float_wire','aggregate_shape',signed_mutation(lambda v:v.update(sequence=1.0)),False),
 ('signed_bool_sequence','aggregate_shape',signed_mutation(lambda v:v.update(sequence=True)),False),
 ('signed_noncanonical_original','aggregate_shape',lambda a,r,s:(r+b' ',a.signed(raw=r+b' ')),False),
 ('signature_over_reencoding','aggregate_signature_invalid',lambda a,r,s:(r,a.signed(raw=json.dumps(a.value,indent=2).encode())),False),
]
for label,reason,change,no_calls in CASES:
 setattr(Trust,'test_refuse_'+label,case(label,reason,change,zero_crypto=no_calls))

if __name__=='__main__':unittest.main(verbosity=2)
