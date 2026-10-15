"""Independent attestation shape is required before current-state comparison."""
import copy
from pathlib import Path
import sys
import unittest
NEXT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(NEXT/'tests/release'))
import test_manifest_current_alignment as original
subjects=original.subjects

class AttestationShape(unittest.TestCase):
 check=original.Alignment.check
 refused=original.Alignment.refused

def malformed(label,empty,change):
 def test(self):
  p,v,c=subjects(empty);change(p['installed_baseline'])
  c.current_state['installed_baseline']=copy.deepcopy(p['installed_baseline'])
  self.refused('required_binding_gap',p,v,c)
 return test
CASES=[
 ('installed_missing_receipt',False,lambda b:b.pop('receipt_sha256')),
 ('installed_boolean_fence',False,lambda b:b.update(fence=True)),
 ('installed_float_fence',False,lambda b:b.update(fence=1.0)),
 ('installed_negative_fence',False,lambda b:b.update(fence=-1)),
 ('installed_fence_over_cap',False,lambda b:b.update(fence=2**63)),
 ('installed_boolean_generation',False,lambda b:b.update(policy_generation=True)),
 ('installed_float_generation',False,lambda b:b.update(policy_generation=1.0)),
 ('installed_zero_generation',False,lambda b:b.update(policy_generation=0)),
 ('installed_generation_over_cap',False,lambda b:b.update(policy_generation=2**63)),
 ('installed_extra_field',False,lambda b:b.update(default=True)),
 ('empty_missing_receipt',True,lambda b:b.pop('receipt_sha256')),
 ('empty_zero_receipt',True,lambda b:b.update(receipt_sha256='0'*64)),
 ('empty_zero_fence',True,lambda b:b.update(fence=0)),
 ('empty_zero_generation',True,lambda b:b.update(policy_generation=0)),
 ('empty_fake_release',True,lambda b:b.update(release_id='v0.0.0')),
 ('empty_extra_field',True,lambda b:b.update(default=True)),
 ('empty_short_receipt',True,lambda b:b.update(receipt_sha256='a')),
]
for label,empty,change in CASES:setattr(AttestationShape,'test_'+label,malformed(label,empty,change))
if __name__=='__main__':unittest.main(verbosity=2)
