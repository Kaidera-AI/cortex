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
from r01u3_support.workspace import workspace_output


def trust():return importlib.import_module('cortex_core.release.trust')


class Companions(unittest.TestCase):
 def refused(self,reason,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: expected typed refusal instead of return/raw dependency exception')
  self.assertEqual(result.reason,reason,'BODY: exact stage reason')
  return result

 def test_repeated_shape_data_avoids_decode_but_rechecks_crypto(self):
  module=trust();auth=factory.test_authority();signature=auth.signed();ctx=auth.context(module)
  with patch.object(module,'decode_json',wraps=module.decode_json) as decode,patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as crypto:
   first=module._verify_original(auth.raw,signature,ctx);second=module._verify_original(auth.raw,signature,ctx)
  self.assertEqual(first.manifest,second.manifest)
  self.assertEqual(decode.call_count,1,'BODY: same immutable shape decoded repeatedly')
  self.assertEqual(crypto.call_count,4,'BODY: a shape memo must never cache signature acceptance')
  with self.assertRaises(TypeError):first.manifest['sequence']=9999

 def test_warm_shape_keeps_owner_time_and_bad_signature_live(self):
  module=trust();auth=factory.test_authority();auth.verify(module)
  auth.now=9999;self.refused('aggregate_replay',lambda:auth.verify(module));auth.now=1500
  lines=auth.signed().split(b'\n');raw=bytearray(base64.b64decode(lines[3]));raw[0]^=1;lines[3]=base64.b64encode(raw)
  with patch.object(module,'verify_ed25519',wraps=module.verify_ed25519) as crypto:
   self.refused('aggregate_signature_invalid',lambda:auth.verify(module,signature=b'\n'.join(lines)))
  self.assertEqual(crypto.call_count,2)
  self.assertEqual(auth.verify(module).manifest_raw,auth.raw)

 def test_warm_shape_obeys_tighter_owner_depth_and_changed_raw(self):
  module=trust();auth=factory.test_authority();auth.verify(module)
  auth.policy['limits']['depth']=1;self.refused('raw_limit',lambda:auth.verify(module))
  auth.policy['limits']['depth']=32
  value=json.loads(auth.raw);value['extra']=True;bad=factory.canonical(value)
  self.refused('aggregate_shape',lambda:auth.verify(module,raw=bad,signature=auth.signed(raw=bad)))
  self.assertEqual(auth.verify(module).manifest_raw,auth.raw)

 def test_warm_shape_does_not_hide_replaced_validator(self):
  module=trust();auth=factory.test_authority();auth.verify(module)
  class Reject:
   def iter_errors(self,value):return iter([object()])
  with patch.object(module,'_MANIFEST_VALIDATOR',Reject()):
   self.refused('aggregate_shape',lambda:auth.verify(module))
  self.assertEqual(auth.verify(module).manifest_raw,auth.raw)

 def test_warm_shape_does_not_hide_changed_schema(self):
  module=trust();auth=factory.test_authority();auth.verify(module)
  sequence=module._MANIFEST_VALIDATOR.schema['properties']['sequence']
  with patch.dict(sequence,{'minimum':auth.value['sequence']+1}):
   self.refused('aggregate_shape',lambda:auth.verify(module))
  self.assertEqual(auth.verify(module).manifest_raw,auth.raw)

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
  with tempfile.TemporaryDirectory(prefix='r01u3-kos-scope-',dir=workspace_output()) as path:
   root=Path(path);(root/'release.json').write_bytes(auth.raw);(root/'release.json.minisig').write_bytes(signature)
   with self.assertRaises(kos.Refusal) as caught:
    kos.verify_package(root,public_key=auth.root_material.decode(),platform='macos-arm64',image_platform='linux/arm64')
  self.assertEqual(caught.exception.code,'release_schema_unsupported','BODY: unchanged KOS consumer admitted Cortex schema')

class PublicCustody(unittest.TestCase):
 """Accepted PUBLIC originals are data; these tests never mint owner authority."""
 HOME=NEXT/'release/custody/2026-10-16'
 PINS={
  'root-custody.json':(2920,'4272c5baff3a3fefee3569803c61ab7093990f49f3fe3ae7768ede62a5b71c47'),
  'custodian-acceptance.json':(1151,'7dfe2923368cef237d420aea28159c123f32f48144e329262e496d50815595ac'),
  'release-public-key.pub':(113,'cc789827d21f6db4b3d74c39709b78d2ab6a636b28489d58d3b7ec442b66c8ef'),
 }

 def reader(self):
  module=trust()
  self.assertTrue(callable(getattr(module,'_public_custody',None)),
                  'BODY: production trust must consume accepted custody state as DATA')
  return module

 def refused(self,fn):
  result=None
  try:fn()
  except Exception as error:result=error
  self.assertIsInstance(result,Refusal,'BODY: custody failure must be a typed refusal')
  self.assertEqual(result.reason,'required_binding_gap','BODY: custody is not an owner provider')
  self.assertEqual(result.unit,'R01 unit 3: signed Core/API aggregate and member admission')
  return result

 def originals(self):return {name:(self.HOME/name).read_bytes() for name in self.PINS}

 def test_accepted_pending_originals_are_read_as_immutable_data(self):
  module=self.reader();state=module._public_custody()
  for name,(size,digest) in self.PINS.items():
   raw=(self.HOME/name).read_bytes()
   self.assertEqual(len(raw),size);self.assertEqual(hashlib.sha256(raw).hexdigest(),digest)
  self.assertEqual(state['purpose_and_permission']['status'],'pending CTO confirmation')
  self.assertIs(state['purpose_and_permission']['production_purpose_authorized'],False)
  self.assertIs(state['purpose_and_permission']['signing_authorized'],False)
  self.assertEqual(state['root']['allowed_purposes'],('cortex/core-api/test',))
  self.assertEqual(state['root']['key_id'],'7BBB4B7E0CE8852E')
  self.assertTrue(all(value is False for value in state['limits'].values()))
  with self.assertRaises(TypeError):state['purpose_and_permission']['signing_authorized']=True

 def test_production_observes_pending_before_candidate_policy_crypto_or_effects(self):
  module=self.reader()
  class Candidate:
   def __len__(self):raise AssertionError('BODY: candidate touched before authority')
  with patch.object(module,'_public_custody',wraps=module._public_custody) as read,patch.object(module,'_owner_policy',side_effect=AssertionError('owner invented')),patch.object(module,'verify_ed25519',side_effect=AssertionError('crypto')),patch('socket.socket',side_effect=AssertionError('socket')),patch('subprocess.Popen',side_effect=AssertionError('process')):
   self.refused(lambda:module.verify_manifest(Candidate(),Candidate()))
  self.assertEqual(read.call_count,1,'BODY: production ignored the pending custody record')
  self.assertIs(module._production_context,module._unbound)

 def test_unsigned_assembly_reads_pending_without_touching_arguments(self):
  module=self.reader();assembly=importlib.import_module('cortex_core.release.assembly')
  class Candidate:
   def __getattribute__(self,name):raise AssertionError('BODY: assembly touched candidate')
  with patch.object(module,'_public_custody',wraps=module._public_custody) as read:
   self.refused(lambda:assembly.assemble_aggregate(Candidate(),Candidate(),Candidate()))
  self.assertEqual(read.call_count,1,'BODY: assembly ignored pending custody')

 def test_every_missing_public_original_is_required_binding_gap(self):
  module=self.reader();original=Path.open
  for missing in self.PINS:
   with self.subTest(missing=missing):
    def opened(path,*args,**kwargs):
     if path==self.HOME/missing:raise FileNotFoundError('SENTINEL_PRIVATE_PATH')
     return original(path,*args,**kwargs)
    with patch.object(Path,'open',opened):error=self.refused(module._public_custody)
    self.assertNotIn('SENTINEL',str(error));self.assertIsNone(error.__cause__)

 def test_changed_truncated_and_extended_originals_are_refused(self):
  from io import BytesIO
  module=self.reader();originals=self.originals();original=Path.open
  for name,raw in originals.items():
   for altered in (raw[:-1],raw+b'x',bytes([raw[0]^1])+raw[1:]):
    with self.subTest(file=name,length=len(altered)):
     def opened(path,*args,**kwargs):
      return BytesIO(altered) if path==self.HOME/name else original(path,*args,**kwargs)
     with patch.object(Path,'open',opened):self.refused(module._public_custody)

 def test_self_rehashed_production_claim_cannot_replace_independent_pins(self):
  from io import BytesIO
  module=self.reader();bodies=self.originals();record=json.loads(bodies['root-custody.json'])
  record['purpose_and_permission'].update(status='confirmed',signing_authorized=True,production_purpose_authorized=True)
  record['root']['allowed_purposes']=['cortex/core-api/production']
  bodies['root-custody.json']=(json.dumps(record,indent=2)+'\n').encode()
  receipt=json.loads(bodies['custodian-acceptance.json'])
  receipt.update(record_sha256=hashlib.sha256(bodies['root-custody.json']).hexdigest(),record_bytes=len(bodies['root-custody.json']),purpose_key_use_confirmation='confirmed',signing_authorized=True)
  bodies['custodian-acceptance.json']=(json.dumps(receipt,indent=2)+'\n').encode()
  def opened(path,*args,**kwargs):
   self.assertEqual(path.parent,self.HOME,'BODY: read followed a record-selected path')
   return BytesIO(bodies[path.name])
  with patch.object(Path,'open',opened):self.refused(module._public_custody)

 def test_pending_and_key_use_are_observed_after_byte_authentication(self):
  module=self.reader();decoded=module.decode_json
  changes=[('purpose_and_permission','status','confirmed'),
           ('purpose_and_permission','signing_authorized',True),
           ('purpose_and_permission','production_purpose_authorized',True),
           ('purpose_and_permission','purpose','cortex/core-api/production'),
           ('purpose_and_permission','key_use_permission','another-key-use'),
           ('root','allowed_purposes',['cortex/core-api/production']),
           ('root','key_use_permission','another-key-use'),
           ('limits','effect_authority',True)]
  for section,key,value in changes:
   with self.subTest(section=section,key=key):
    def decode(raw,*args,**kwargs):
     result=decoded(raw,*args,**kwargs)
     if result.get('record_type')=='cortex.release.public-root-custody':result[section][key]=value
     return result
    with patch.object(module,'decode_json',decode):self.refused(module._public_custody)

 def test_custodian_confirmation_is_observed_independently(self):
  module=self.reader();decoded=module.decode_json
  for key,value in [('purpose_key_use_confirmation','confirmed'),('signing_authorized',True),('effect_authority',True)]:
   with self.subTest(key=key):
    def decode(raw,*args,**kwargs):
     result=decoded(raw,*args,**kwargs)
     if result.get('record_type')=='cortex.release.custodian-material-acceptance':result[key]=value
     return result
    with patch.object(module,'decode_json',decode):self.refused(module._public_custody)

 def test_reads_have_finite_byte_bounds_and_close_each_original(self):
  from io import BytesIO
  module=self.reader();bodies=self.originals();streams=[]
  class Original(BytesIO):
   def read(stream,size=-1):
    self.assertGreater(size,0,'BODY: unbounded custody read')
    self.assertLessEqual(size,2921,'BODY: read exceeds largest original bound')
    stream.read_sizes.append(size)
    return super().read(size)
  def opened(path,mode='r',*args,**kwargs):
   self.assertEqual(path.parent,self.HOME);self.assertEqual(mode,'rb')
   stream=Original(bodies[path.name]);stream.read_sizes=[];streams.append((path.name,stream));return stream
  with patch.object(Path,'open',opened):module._public_custody()
  self.assertCountEqual([name for name,_ in streams],self.PINS)
  for name,stream in streams:
   self.assertEqual(stream.read_sizes,[self.PINS[name][0]+1])
   self.assertTrue(stream.closed,'BODY: custody descriptor leaked')

 def test_custody_is_reloaded_after_a_successful_data_read(self):
  module=self.reader();module._public_custody()
  with patch.object(Path,'open',side_effect=OSError('SENTINEL_UNAVAILABLE')):
   self.refused(module._public_custody)
   self.refused(lambda:module.verify_manifest(b'candidate',b'candidate'))

 def test_shared_root_parser_checks_the_complete_original(self):
  module=self.reader()
  self.assertTrue(callable(getattr(module,'_root_material',None)),'BODY: reuse sole public-root parser without owner context')
  with patch.object(module,'_root_material',wraps=module._root_material) as parse:
   state=module._public_custody()
  self.assertEqual(parse.call_count,1)
  self.assertEqual(parse.call_args.args[0],(self.HOME/'release-public-key.pub').read_bytes())
  self.assertEqual(parse.call_args.args[1]['key_id'],state['root']['key_id'])
  with patch.object(module,'_root_material',side_effect=Refusal('aggregate_root_mismatch')):
   self.refused(module._public_custody)

 def test_argv_environment_and_cwd_cannot_select_custody(self):
  import os
  module=self.reader();original=Path.open;seen=[]
  def opened(path,*args,**kwargs):seen.append(path);return original(path,*args,**kwargs)
  with patch.dict(os.environ,{'CORTEX_CUSTODY_ROOT':'/candidate/forged','CORTEX_RELEASE_PUBLIC_KEY':'/candidate/key'}),patch.object(sys,'argv',['candidate','--custody','/candidate/forged']),patch('os.getcwd',return_value='/candidate'),patch.object(Path,'open',opened):
   state=module._public_custody()
  self.assertEqual(set(seen),{self.HOME/name for name in self.PINS})
  self.assertFalse(state['limits']['production_owner_context_bound'])

if __name__=='__main__':unittest.main(verbosity=2)
