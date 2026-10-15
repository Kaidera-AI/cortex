"""RFC8032 public known answers + original Wycheproof-style negatives.

No secret seed/key file. This is a bounded adversarial subset, not the full
Wycheproof corpus or product/native qualification. Source: RFC8032 section7.1.
"""
import importlib
from pathlib import Path
import sys
import unittest

NEXT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'src'))
VECTORS=[
 ('rfc8032_1','d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a','',
  'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
  '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
 ('rfc8032_2','3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c','72',
  '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da'
  '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
 ('rfc8032_3','fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025','af82',
  '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac'
  '18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
]
L=2**252+27742317777372353535851937790883648493
P=2**255-19


def product():
 try:return importlib.import_module('cortex_core.release.ed25519')
 except ModuleNotFoundError as error:
  if error.name!='cortex_core.release.ed25519':raise
  return None


class Provider(unittest.TestCase):
 def verify(self,key,msg,sig):
  module=product();self.assertIsNotNone(module,'SURFACE_ABSENCE: ONE Ed25519 provider boundary')
  self.assertTrue(callable(getattr(module,'verify_ed25519',None)),'SURFACE_ABSENCE: verify_ed25519')
  result=module.verify_ed25519(key,msg,sig)
  self.assertIs(type(result),bool,'BODY: provider result must be exact bool')
  return result


def positive(vector):
 def test(self):
  _,key,msg,sig=vector
  self.assertTrue(self.verify(bytes.fromhex(key),bytes.fromhex(msg),bytes.fromhex(sig)),
                  'BODY: RFC8032 authentic known answer refused')
 return test


def negative(label,make):
 def test(self):
  key=bytes.fromhex(VECTORS[0][1]);msg=b'';sig=bytes.fromhex(VECTORS[0][3])
  key,msg,sig=make(key,msg,sig)
  self.assertFalse(self.verify(key,msg,sig),'BODY: Wycheproof-style '+label+' accepted')
 return test


for vector in VECTORS:setattr(Provider,'test_'+vector[0],positive(vector))
CASES={
 'changed_message':lambda k,m,s:(k,b'changed',s),
 'changed_signature':lambda k,m,s:(k,m,bytes([s[0]^1])+s[1:]),
 'changed_public_key':lambda k,m,s:(bytes([k[0]^1])+k[1:],m,s),
 'signature_short':lambda k,m,s:(k,m,s[:-1]),
 'signature_long':lambda k,m,s:(k,m,s+b'x'),
 'key_short':lambda k,m,s:(k[:-1],m,s),
 'key_long':lambda k,m,s:(k+b'x',m,s),
 'S_equals_L':lambda k,m,s:(k,m,s[:32]+L.to_bytes(32,'little')),
 'S_plus_L':lambda k,m,s:(k,m,s[:32]+(int.from_bytes(s[32:],'little')+L).to_bytes(32,'little')),
 'S_max':lambda k,m,s:(k,m,s[:32]+b'\xff'*32),
 'R_noncanonical_y_p':lambda k,m,s:(k,m,P.to_bytes(32,'little')+s[32:]),
 'R_noncanonical_y_p_plus_one':lambda k,m,s:(k,m,(P+1).to_bytes(32,'little')+s[32:]),
 'pk_noncanonical_y_p':lambda k,m,s:(P.to_bytes(32,'little'),m,s),
 'pk_noncanonical_y_p_plus_one':lambda k,m,s:((P+1).to_bytes(32,'little'),m,s),
 'key_type_str':lambda k,m,s:(k.hex(),m,s),
 'message_type_str':lambda k,m,s:(k,'',s),
 'signature_type_str':lambda k,m,s:(k,m,s.hex()),
 'key_bytearray':lambda k,m,s:(bytearray(k),m,s),
 'message_bytearray':lambda k,m,s:(k,bytearray(m),s),
 'signature_bytearray':lambda k,m,s:(k,m,bytearray(s)),
}
SMALL=[b'\0'*32,b'\1'+b'\0'*31,(P-1).to_bytes(32,'little')]
for i,point in enumerate(SMALL):
 CASES['small_order_pk_'+str(i)]=lambda k,m,s,point=point:(point,m,s)
 CASES['small_order_R_'+str(i)]=lambda k,m,s,point=point:(k,m,point+s[32:])
 CASES['small_order_forgery_'+str(i)]=lambda k,m,s,point=point:(point,b'forged',point+b'\0'*32)
for label,make in CASES.items():setattr(Provider,'test_reject_'+label,negative(label,make))

if __name__=='__main__':unittest.main(verbosity=2)
