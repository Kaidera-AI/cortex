"""Frozen provider strict-encoding guards; original RFC/subset stays byte exact."""
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from test_ed25519_provider import VECTORS
P=2**255-19
POINTS=(0,1,2707385501144840649318225287225658788936804267575313519463743609750303402022,
 55188659117513257062467267217118295137698188065244968500265048394206261417927,P-1,P,P+1,2**255-1)

class StrictEncoding(unittest.TestCase):
 pass
def case(point,sign,part):
 def test(self):
  from cryptography.hazmat.primitives.asymmetric import ed25519
  module=importlib.import_module('cortex_core.release.ed25519')
  key=bytes.fromhex(VECTORS[0][1]);signature=bytes.fromhex(VECTORS[0][3])
  encoded=(point|(sign<<255)).to_bytes(32,'little')
  if part=='key':key=encoded
  else:signature=encoded+signature[32:]
  # A permissive native stub isolates the bounded input guard. Actual-primitive
  # RFC and subset controls are separately retained and executed unchanged.
  with patch.object(ed25519,'Ed25519PublicKey') as primitive:
   result=module.verify_ed25519(key,b'',signature)
   self.assertIs(result,False,'BODY: forbidden encoded point reached verification')
   self.assertEqual(primitive.from_public_bytes.call_count,0,'BODY: point guard omitted')
 return test
for i,point in enumerate(POINTS):
 for sign in (0,1):
  for part in ('key','R'):
   setattr(StrictEncoding,'test_point_'+str(i)+'_'+str(sign)+'_'+part,case(point,sign,part))
if __name__=='__main__':unittest.main(verbosity=2)
