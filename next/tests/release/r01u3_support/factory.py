"""TEST ONLY. Fresh unrelated signing material lives in RAM, never a key file.

Factory is outside installed source. It creates only signature-test context,
never OriginalMember, owner resources, native qualification or effect authority.
"""
import base64
import copy
import hashlib
import inspect
import json
from pathlib import Path
from types import MappingProxyType


def reject_product_caller():
 frame=inspect.currentframe().f_back
 while frame:
  if '/src/cortex_core/' in frame.f_code.co_filename:
   raise ImportError('test_support_forbidden')
  frame=frame.f_back


reject_product_caller()
from nacl.signing import SigningKey

HOME=Path(__file__).resolve().parent
RELEASE_ID='7BBB4B7E0CE8852E'


def canonical(value):
 return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()


def comment(raw,value):
 return ('cortex.release-aggregate.v1|purpose='+value['purpose']+'|release='+value['release']['release_id']+
         '|sequence='+str(value['sequence'])+'|sha256='+hashlib.sha256(raw).hexdigest()+'|source='+value['source']['commit'])


class TestAuthority:
 def __init__(self):
  reject_product_caller()
  # Caller must be a test; signing material is neither persisted nor logged.
  self._signer=SigningKey.generate()
  self.public=bytes(self._signer.verify_key)
  self.key_id=hashlib.sha256(self.public).digest()[:8]
  if self.key_id[::-1].hex().upper()==RELEASE_ID:raise AssertionError('test root collided with release ID')
  self.root_material=(b'untrusted comment: TEST ONLY unrelated root\n'+base64.b64encode(b'Ed'+self.key_id+self.public)+b'\n')
  self.raw=(HOME/'aggregate.canonical.json').read_bytes()
  self.value=json.loads(self.raw)
  self.policy=json.loads((HOME/'production-policy.shape.json').read_bytes())
  self.policy['root'].update(key_id=self.key_id[::-1].hex().upper(),key_material_sha256=hashlib.sha256(self.root_material).hexdigest(),public_key_bytes=len(self.root_material))
  self.policy['expected_aggregate_sha256']=hashlib.sha256(self.raw).hexdigest()
  self.policy['release']=copy.deepcopy(self.value['release']);self.policy['source']=copy.deepcopy(self.value['source'])
  self.now=1500;self.generation=1;self.fence=1;self.placement='installer'

 def signed(self,raw=None,*,algorithm=b'ED',trusted_comment=None,signature_key_id=None,untrusted='test-only envelope'):
  reject_product_caller()
  raw=self.raw if raw is None else raw
  text=comment(raw,json.loads(raw)) if trusted_comment is None else trusted_comment
  message=hashlib.blake2b(raw,digest_size=64).digest() if algorithm==b'ED' else raw
  first=self._signer.sign(message).signature
  second=self._signer.sign(first+text.encode('utf-8')).signature
  key_id=self.key_id if signature_key_id is None else signature_key_id
  return ('untrusted comment: '+untrusted+'\n'+base64.b64encode(algorithm+key_id+first).decode()+
          '\ntrusted comment: '+text+'\n'+base64.b64encode(second).decode()+'\n').encode()

 def context(self,trust):
  reject_product_caller()
  from cortex_core.release.contracts import freeze
  raw=canonical(self.policy)
  return trust._OwnerContext(freeze(copy.deepcopy(self.policy)),raw,hashlib.sha256(raw).hexdigest(),
                             self.root_material,self.now,self.generation,self.fence,self.placement)

 def verify(self,trust,raw=None,signature=None):
  reject_product_caller()
  return trust._verify_original(self.raw if raw is None else raw,
                                self.signed() if signature is None else signature,self.context(trust))

 def __repr__(self):return '<TestAuthority: unrelated in-memory root; no private bytes displayed>'


def test_authority():
 reject_product_caller()
 return TestAuthority()
