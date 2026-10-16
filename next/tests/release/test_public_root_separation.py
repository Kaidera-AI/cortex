"""H-D518 TEST-root separation on the ACTUAL public entry (Focus group A1).

The public installer entry verify_manifest admits ONE baked root: the exact
published CTO minisign public bytes (key 7BBB4B7E0CE8852E, file sha cc789827...,
the same bytes KOS bakes). Any other root - an internal TEST key, a key that
claims the CTO short key ID, or owner-delivered material - refuses before
member parse. Keys are disposable, generated in RAM by the test factory only.
"""
import ast
import base64
from dataclasses import replace
import hashlib
import importlib
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory

# Published CTO public half (KOS scripts/release/installer/kos_installer/release-public-key.pub).
PUBLISHED=(b'untrusted comment: minisign public key 7BBB4B7E0CE8852E\n'
           b'RWQuhegMfku7e4RltjV64sZmxXHEETzAntDePCQsJPYvXXujVMqKIvHL\n')
PUBLISHED_SHA256='cc789827d21f6db4b3d74c39709b78d2ab6a636b28489d58d3b7ec442b66c8ef'
CTO_ID=bytes.fromhex('7BBB4B7E0CE8852E')[::-1]


def module():return importlib.import_module('cortex_core.release.trust')


def same_id_forgery():
 """A disposable key that CLAIMS the CTO key ID; owner policy pins its bytes."""
 auth=factory.test_authority()
 auth.key_id=CTO_ID
 auth.root_material=b'untrusted comment: minisign public key 7BBB4B7E0CE8852E\n'+base64.b64encode(b'Ed'+CTO_ID+auth.public)+b'\n'
 auth.policy['root'].update(key_id='7BBB4B7E0CE8852E',key_material_sha256=hashlib.sha256(auth.root_material).hexdigest(),
                            public_key_bytes=len(auth.root_material))
 return auth


def internal_root():
 """A disposable internal TEST key with its own ID (the H-D518 internal flavour)."""
 return factory.test_authority()


class PublicRootSeparation(unittest.TestCase):
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: public entry accepted or leaked a non-refusal')
  self.assertEqual(result.reason,reason,'BODY: exact root refusal')

 def no_parse_no_crypto(self,trust,fn,reason):
  with patch.object(trust,'verify_ed25519',wraps=trust.verify_ed25519) as crypto,\
       patch.object(trust,'_manifest_shape',wraps=trust._manifest_shape) as parse:
   self.refused(reason,fn)
  self.assertEqual(crypto.call_count,0,'BODY: crypto ran on a foreign root')
  self.assertEqual(parse.call_count,0,'BODY: candidate manifest parsed before the root refusal')

 def test_A1_baked_root_is_the_exact_published_cto_public_half(self):
  trust=module()
  self.assertEqual(getattr(trust,'PRODUCTION_ROOT',None),PUBLISHED,'BODY: public entry has no baked CTO root bytes')
  self.assertEqual(hashlib.sha256(trust.PRODUCTION_ROOT).hexdigest(),PUBLISHED_SHA256)
  # The baked bytes parse through the SAME root reader to the CTO ID.
  self.assertEqual(base64.b64decode(PUBLISHED.split(b'\n')[1])[2:10],CTO_ID)

 def test_A2_generic_algorithm_accepts_the_same_id_forgery_positive(self):
  # Causal positive: the forged envelope is otherwise fully valid, so A3's
  # refusal comes from the baked-root pin, not a missing field.
  trust=module();auth=same_id_forgery()
  result=trust._verify_original(auth.raw,auth.signed(),auth.context(trust))
  self.assertEqual(result.signing_key_id,'7BBB4B7E0CE8852E')

 def test_A3_public_entry_refuses_same_id_forgery_even_when_owner_pins_it(self):
  trust=module();auth=same_id_forgery()
  with patch.object(trust,'_production_context',return_value=auth.context(trust)):
   self.no_parse_no_crypto(trust,lambda:trust.verify_manifest(auth.raw,auth.signed()),'aggregate_root_mismatch')

 def test_A4_public_entry_refuses_internal_root_delivered_as_owner_root(self):
  trust=module();auth=internal_root()
  with patch.object(trust,'_production_context',return_value=auth.context(trust)):
   self.no_parse_no_crypto(trust,lambda:trust.verify_manifest(auth.raw,auth.signed()),'aggregate_root_mismatch')

 def test_A5_internal_signature_against_baked_root_and_cto_policy(self):
  # Owner context carries the exact baked CTO root and a policy pinning it;
  # the candidate is signed by the internal TEST key (own ID, then claimed ID).
  trust=module();auth=internal_root()
  auth.policy['root'].update(key_id='7BBB4B7E0CE8852E',key_material_sha256=PUBLISHED_SHA256,public_key_bytes=len(PUBLISHED))
  context=replace(auth.context(trust),public_root=PUBLISHED)
  with patch.object(trust,'_production_context',return_value=context):
   self.refused('aggregate_root_mismatch',lambda:trust.verify_manifest(auth.raw,auth.signed()))
   with patch.object(trust,'_manifest_shape',wraps=trust._manifest_shape) as parse:
    self.refused('aggregate_signature_invalid',lambda:trust.verify_manifest(auth.raw,auth.signed(signature_key_id=CTO_ID)))
   self.assertEqual(parse.call_count,0,'BODY: candidate parsed before the signature refusal')

 def test_A6_relabelling_never_helps_a_foreign_root(self):
  trust=module();auth=same_id_forgery()
  for purpose in ('cortex/core-api/test','cortex/core-api/production'):
   auth.policy['purpose']=purpose;auth.policy['root']['allowed_purposes']=[purpose]
   with patch.object(trust,'_production_context',return_value=auth.context(trust)):
    self.refused('aggregate_root_mismatch',lambda:trust.verify_manifest(auth.raw,auth.signed()))

 def test_A7_root_is_not_selectable_at_runtime(self):
  trust=module()
  self.assertEqual(list(inspect.signature(trust.verify_manifest).parameters),['raw','signature'])
  tree=ast.parse((NEXT/'src/cortex_core/release/trust.py').read_text())
  names={n.id for n in ast.walk(tree) if isinstance(n,ast.Name)}|{n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)}
  for banned in ('environ','getenv','argv','urlopen','expanduser'):
   self.assertNotIn(banned,names,'BODY: root/flavour selectable by '+banned)

 def test_A8_installed_source_carries_exactly_one_root_identity(self):
  # Separate baked roots: no internal/second root, key ID or material digest
  # may ride in the public installed tree (an internal flavour is a separate
  # packaging closure, never a switch inside this one).
  ids=set();keys=set()
  for path in sorted((NEXT/'src').rglob('*.py')):
   text=path.read_text(encoding='utf-8')
   for node in ast.walk(ast.parse(text)):
    if isinstance(node,ast.Constant) and isinstance(node.value,(str,bytes)):
     value=node.value.decode('latin-1') if isinstance(node.value,bytes) else node.value
     if 'minisign public key' in value or value.startswith('RWQ'):keys.add((path.name,value))
     if len(value)==16 and value.isupper() and all(c in '0123456789ABCDEF' for c in value):ids.add(value)
  self.assertEqual(ids,{'7BBB4B7E0CE8852E'},'BODY: second key ID in installed source')
  self.assertEqual({name for name,_ in keys},{'trust.py'},'BODY: root material outside the sole verifier')


if __name__=='__main__':unittest.main(verbosity=2)
