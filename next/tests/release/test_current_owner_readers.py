"""D2 source-only DATA probes; no fixture is installed as production authority."""
import copy
from dataclasses import FrozenInstanceError, replace
import importlib
import io
from pathlib import Path
import sys
from threading import Event,Thread
import unittest
from unittest.mock import patch

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal
from r01u3_support import factory


class CurrentOwnerReaders(unittest.TestCase):
 def module(self):
  m=importlib.import_module('cortex_core.release.trust')
  for name in ('_precut_policy','_CurrentOwner','_installed_current_owner',
               '_read_current_owner','_current_owner_reader'):
   self.assertTrue(callable(getattr(m,name,None)),'BODY: missing D2 reader '+name)
  return m

 def refused(self,fn,reason='required_binding_gap'):
  error=None
  try:fn()
  except Exception as caught:error=caught
  self.assertIsInstance(error,Refusal,'BODY: reader admitted or leaked an untyped dependency error')
  self.assertEqual(error.reason,reason,'BODY: exact current-owner refusal')
  return error

 def sample(self,m,*,empty=False):
  # Inherited policy shape, with the pinned PUBLIC root DATA for full schema.
  # No signature or owner provenance is manufactured; production still refuses.
  a=factory.test_authority()
  public=(NEXT/'release/custody/2026-10-16/release-public-key.pub').read_bytes()
  a.root_material=public
  a.policy['root'].update(key_id=m.PRODUCTION_KEY_ID,public_key_bytes=len(public),
                         key_material_sha256=m.sha256(public))
  if empty:
   a.policy['installed_baseline']=dict(state='empty',receipt_sha256='e'*64,fence=1,policy_generation=1)
   a.current_state['installed_baseline']=copy.deepcopy(a.policy['installed_baseline'])
  return m._CurrentOwner(a.context(m),a.policy['sequence_floor'],7)

 def changed_policy(self,m,sample,change):
  policy=m.thaw(sample.context.policy);state=m.thaw(sample.context.current_state)
  change(policy,state)
  raw=m.canonical_bytes(policy)
  return replace(sample,context=replace(sample.context,policy=m.freeze(policy),policy_raw=raw,
      policy_sha256=m.sha256(raw),current_state=m.freeze(state)))

 def read(self,m,sample):
  with patch.object(m,'_installed_current_owner',return_value=sample):
   return m._read_current_owner()

 def test_precut_originals_remain_immutable_schema_invalid_data(self):
  m=self.module();p=m._precut_policy()
  self.assertEqual(p['sequence_floor'],'bound at the cut')
  self.assertEqual(p['installed_baseline'],'bound at the cut')
  self.assertEqual(len(list(m._POLICY_VALIDATOR.iter_errors(m.thaw(p)))),15)
  self.assertEqual(p['owner_binding']['root_store_sha256'],
                   'f61e63cb4e49586d263d875f0db19fd9b79b26a4c893c3a04a6168173006eb6b')
  with self.assertRaises(TypeError):p['sequence_floor']=1

 def test_each_precut_original_is_pinned_bounded_closed_and_reloaded(self):
  m=self.module();home=NEXT/'release/custody/2026-10-16'
  names=('aggregate-policy.json','root-store-permission.json','owner-policy-custodian-acceptance.json')
  originals={name:(home/name).read_bytes() for name in names}
  opened=[];reads=[]
  class Stream(io.BytesIO):
   def __init__(self,name,raw):self.name=name;super().__init__(raw);opened.append(self)
   def read(self,n=-1):reads.append((self.name,n));return super().read(n)
  def open_original(path,*args,**kwargs):
   self.assertEqual(path.parent,home);self.assertEqual(args,('rb',))
   return Stream(path.name,originals[path.name])
  with patch.object(Path,'open',open_original):m._precut_policy();m._precut_policy()
  self.assertEqual(reads,[(n,len(originals[n])+1) for n in names]*2)
  self.assertTrue(all(s.closed for s in opened),'BODY: original descriptor leaked')
  for name in names:
   for replacement in (None,originals[name][:-1],originals[name]+b'x',b'x'+originals[name][1:]):
    with self.subTest(name=name,kind=None if replacement is None else len(replacement)):
     def changed(path,*args,**kwargs):
      if path.name==name and replacement is None:raise OSError('PRIVATE_PATH_SENTINEL')
      return io.BytesIO(replacement if path.name==name else originals[path.name])
     with patch.object(Path,'open',changed):
      e=self.refused(m._precut_policy);self.assertNotIn('SENTINEL',str(e))

 def test_semantically_valid_same_size_receipt_replacement_is_refused(self):
  m=self.module();home=NEXT/'release/custody/2026-10-16'
  names=('aggregate-policy.json','root-store-permission.json','owner-policy-custodian-acceptance.json')
  originals={name:(home/name).read_bytes() for name in names}
  name=names[2];changed=originals[name].replace(b'2026-10-16T06:10:38',b'2026-10-16T06:10:39')
  self.assertNotEqual(changed,originals[name]);self.assertEqual(len(changed),len(originals[name]))
  # Valid canonical DATA with identical permission/joins still is not the original.
  self.assertEqual(m.canonical_bytes(m.decode_json(changed,len(changed))),changed)
  def replaced(path,*args,**kwargs):
   return io.BytesIO(changed if path.name==name else originals[path.name])
  with patch.object(Path,'open',replaced):self.refused(m._precut_policy)

 def test_precut_pending_permission_and_digest_joins_are_observed(self):
  m=self.module();decode=m.decode_json
  faults=[('aggregate-policy.json',lambda v:v['owner_binding'].update(root_store_sha256='0'*64)),
          ('root-store-permission.json',lambda v:v['limits'].update(permission_active=True)),
          ('root-store-permission.json',lambda v:v['purpose_and_permission'].update(signing_authorized=True)),
          ('root-store-permission.json',lambda v:v['purpose_and_permission'].update(status='confirmed')),
          ('root-store-permission.json',lambda v:v['roots'][0].update(key_id='0'*16)),
          ('owner-policy-custodian-acceptance.json',lambda v:v.update(policy_raw_sha256='0'*64)),
          ('owner-policy-custodian-acceptance.json',lambda v:v.update(root_store_sha256='0'*64)),
          ('owner-policy-custodian-acceptance.json',lambda v:v.update(canonical_policy_schema_admitted=True)),
          ('owner-policy-custodian-acceptance.json',lambda v:v.update(signing_authorized=True))]
  originals={name:(NEXT/'release/custody/2026-10-16'/name).read_bytes() for name,_ in faults}
  for name,change in faults:
   with self.subTest(name=name,change=change):
    def changed(raw,*args,**kwargs):
     value=decode(raw,*args,**kwargs)
     if raw==originals[name]:change(value)
     return value
    with patch.object(m,'decode_json',changed):self.refused(m._precut_policy)

 def test_precut_decode_failure_is_sanitized(self):
  m=self.module()
  with patch.object(m,'decode_json',side_effect=ValueError('PRIVATE_SENTINEL')):
   e=self.refused(m._precut_policy)
  self.assertNotIn('SENTINEL',str(e));self.assertIsNone(e.__cause__)

 def test_public_entrypoints_still_refuse_even_when_private_data_is_supplied(self):
  m=self.module();sample=self.sample(m)
  self.assertIs(m._production_context,m._unbound)
  from cortex_core.release import assembly
  for call in (lambda:m.verify_manifest(object(),object()),
               lambda:assembly.assemble_aggregate(object(),object(),object())):
   with patch.object(m,'_public_custody',return_value={}),patch.object(m,'_precut_policy',return_value={}) as data,\
        patch.object(m,'_read_current_owner',return_value=sample) as current,\
        patch.object(m,'_owner_policy',side_effect=AssertionError('candidate policy reached')):
    self.refused(call)
   self.assertEqual(data.call_count,1);self.assertEqual(current.call_count,1)

 def test_fixed_installed_provider_has_no_fallback_or_selector(self):
  m=self.module()
  with patch.dict('os.environ',{'CORTEX_OWNER_CLOCK':'1500','CORTEX_BASELINE':'empty',
                              'CORTEX_FENCE':'1','CORTEX_RESTART_EPOCH':'7'}),\
       patch.object(sys,'argv',['candidate','--owner-state=/tmp/fixture']),\
       patch('time.time',side_effect=AssertionError('system clock is not an admitted owner')):
   self.refused(m._installed_current_owner);self.refused(m._read_current_owner)
   self.refused(m._current_owner_reader)

 def test_valid_private_observation_is_frozen_data(self):
  m=self.module();sample=self.sample(m);read=self.read(m,sample)
  self.assertEqual(read.context.policy_raw,sample.context.policy_raw)
  self.assertEqual(read.context.now,1500);self.assertEqual(read.restart_epoch,7)
  self.assertEqual(read.sequence_floor,sample.context.policy['sequence_floor'])
  with self.assertRaises(FrozenInstanceError):read.restart_epoch=8
  with self.assertRaises(TypeError):read.context.current_state['fence']=999
  self.assertFalse(hasattr(read,'effect_authority'))

 def test_mutable_input_graph_is_detached(self):
  m=self.module();sample=self.sample(m)
  policy=m.thaw(sample.context.policy);state=m.thaw(sample.context.current_state)
  sample=replace(sample,context=replace(sample.context,policy=policy,current_state=state))
  observed=self.read(m,sample);policy['sequence_floor']=999;state['fence']=999
  self.assertNotEqual(observed.context.policy['sequence_floor'],999)
  self.assertNotEqual(observed.context.current_state['fence'],999)

 def test_required_record_and_counter_types_never_default(self):
  m=self.module();sample=self.sample(m)
  for bad in (None,{},object(),replace(sample,context=None)):
   with self.subTest(bad=type(bad)):self.refused(lambda:self.read(m,bad))
  for field in ('sequence_floor','restart_epoch'):
   for value in (None,False,True,0,-1,2**63,'1'):
    with self.subTest(field=field,value=value):
     self.refused(lambda:self.read(m,replace(sample,**{field:value})))

 def test_full_policy_canonical_hash_and_schema_are_required(self):
  m=self.module();s=self.sample(m)
  for c in (replace(s.context,policy_sha256='f'*64),
            replace(s.context,policy_raw=s.context.policy_raw+b' ')):
   self.refused(lambda:self.read(m,replace(s,context=c)))
  bad=self.changed_policy(m,s,lambda p,st:p.update(sequence_floor='bound at the cut'))
  self.refused(lambda:self.read(m,bad))

 def test_complete_public_root_parser_is_reused(self):
  m=self.module();s=self.sample(m)
  with patch.object(m,'_root_material',wraps=m._root_material) as root:self.read(m,s)
  self.assertEqual(root.call_count,1)
  self.refused(lambda:self.read(m,replace(s,context=replace(s.context,public_root=b'x'))),
               'aggregate_root_mismatch')

 def test_baseline_absence_is_not_fresh_attestation(self):
  m=self.module();s=self.sample(m)
  for state in (None,{},dict(policy_generation=1,fence=1),
                dict(installed_baseline=None,policy_generation=1,fence=1)):
   self.refused(lambda:self.read(m,replace(s,context=replace(s.context,current_state=state))))
  self.assertEqual(self.read(m,self.sample(m,empty=True)).context.current_state['installed_baseline']['state'],'empty')

 def test_baseline_alignment_is_required(self):
  m=self.module();s=self.sample(m);state=m.thaw(s.context.current_state)
  state['installed_baseline']['receipt_sha256']='f'*64
  self.refused(lambda:self.read(m,replace(s,context=replace(s.context,current_state=state))),'aggregate_replay')

 def test_current_generation_and_fence_are_required(self):
  m=self.module();s=self.sample(m)
  for field in ('generation','fence'):
   for value in (None,True,'1',2):
    with self.subTest(field=field,value=value):
     self.refused(lambda:self.read(m,replace(s,context=replace(s.context,**{field:value}))),
                  'aggregate_replay')

 def test_trusted_clock_has_exact_type_and_original_validity(self):
  m=self.module();s=self.sample(m);v=s.context.policy['validity']
  for now in (None,False,True,'1500',v['not_before']-1,v['expires_at']+1):
   with self.subTest(now=now):
    self.refused(lambda:self.read(m,replace(s,context=replace(s.context,now=now))),'aggregate_replay')
  for now in (v['not_before'],v['expires_at']):
   self.assertEqual(self.read(m,replace(s,context=replace(s.context,now=now))).context.now,now)

 def test_sequence_floor_must_equal_independent_current_observation(self):
  m=self.module();s=self.sample(m)
  for floor in (s.sequence_floor-1,s.sequence_floor+1):
   with self.subTest(floor=floor):
    self.refused(lambda:self.read(m,replace(s,sequence_floor=floor)),
                 'required_binding_gap' if floor==0 else 'aggregate_replay')

 def test_retained_reader_reloads_on_every_read(self):
  m=self.module();s=self.sample(m);new=replace(s,context=replace(s.context,now=1501))
  with patch.object(m,'_installed_current_owner',side_effect=[s,new,new]) as provider:
   current=m._current_owner_reader()
   self.assertEqual(current().context.now,1501);self.assertEqual(current().context.now,1501)
  self.assertEqual(provider.call_count,3,'BODY: current owner observation was memoized')

 def test_one_retained_reader_serializes_concurrent_source_observations(self):
  m=self.module();s=self.sample(m);first=Event();release=Event();second=Event();errors=[];calls=[]
  def provider():
   calls.append(len(calls)+1);n=calls[-1]
   if n==2:
    first.set()
    if not release.wait(5):raise RuntimeError('test worker was not released')
   if n==3:second.set()
   return replace(s,context=replace(s.context,now=1499+n))
  def invoke(current):
   try:current()
   except BaseException as e:errors.append(e)
  with patch.object(m,'_installed_current_owner',provider):
   current=m._current_owner_reader();a=Thread(target=invoke,args=(current,));b=Thread(target=invoke,args=(current,))
   a.start()
   try:
    self.assertTrue(first.wait(5),'BODY: first current observation did not start')
    b.start()
    self.assertFalse(second.wait(.2),'BODY: one retained reader sampled two owner states concurrently')
   finally:
    release.set();a.join(5)
    if b.ident is not None:b.join(5)
   self.assertFalse(a.is_alive() or b.is_alive());self.assertEqual(errors,[])
   self.assertEqual(calls,[1,2,3])

 def test_clock_rollback_after_advancing_invalidates_retained_reader(self):
  m=self.module();s=self.sample(m);new=replace(s,context=replace(s.context,now=1502))
  with patch.object(m,'_installed_current_owner',side_effect=[s,new,s]):
   current=m._current_owner_reader();current()
   self.refused(current,'aggregate_replay')

 def test_restart_invalidates_retained_reader(self):
  m=self.module();s=self.sample(m)
  with patch.object(m,'_installed_current_owner',side_effect=[s,replace(s,restart_epoch=8)]):
   current=m._current_owner_reader();self.refused(current,'aggregate_replay')

 def test_coherent_generation_fence_baseline_sequence_and_policy_replacements_invalidate(self):
  m=self.module();s=self.sample(m);changes=[]
  generation=self.changed_policy(m,s,lambda p,st:(p['owner_binding'].update(policy_generation=2),st.update(policy_generation=2)))
  changes.append(replace(generation,context=replace(generation.context,generation=2)))
  state=m.thaw(s.context.current_state);state['fence']=2
  changes.append(replace(s,context=replace(s.context,fence=2,current_state=state)))
  changes.append(self.changed_policy(m,s,lambda p,st:(p['installed_baseline'].update(receipt_sha256='f'*64),st['installed_baseline'].update(receipt_sha256='f'*64))))
  sequence=self.changed_policy(m,s,lambda p,st:p.update(sequence_floor=s.sequence_floor+1))
  changes.append(replace(sequence,sequence_floor=s.sequence_floor+1))
  changes.append(self.changed_policy(m,s,lambda p,st:p['source'].update(commit='f'*40)))
  for fresh in changes:
   with self.subTest(fresh=fresh.context.policy_sha256):
    self.read(m,fresh) # coherent independently admissible DATA, sole fault is replacement
    with patch.object(m,'_installed_current_owner',side_effect=[s,fresh]):
     current=m._current_owner_reader();self.refused(current,'aggregate_replay')

 def test_expiry_during_retained_read_refuses_without_extending_window(self):
  m=self.module();s=self.sample(m)
  expired=replace(s,context=replace(s.context,now=s.context.policy['validity']['expires_at']+1))
  with patch.object(m,'_installed_current_owner',side_effect=[s,expired]):
   current=m._current_owner_reader();self.refused(current,'aggregate_replay')

 def test_unavailable_source_permanently_invalidates_retained_reader(self):
  m=self.module();s=self.sample(m)
  with patch.object(m,'_installed_current_owner',side_effect=[s,Refusal('required_binding_gap'),s]) as provider:
   current=m._current_owner_reader();self.refused(current)
   self.refused(current,'aggregate_replay')
  self.assertEqual(provider.call_count,2,'BODY: revoked reader recovered after unavailable observation')

 def test_dependency_error_is_sanitized_and_latched(self):
  m=self.module();s=self.sample(m)
  with patch.object(m,'_installed_current_owner',side_effect=[s,RuntimeError('PRIVATE_SENTINEL'),s]) as provider:
   current=m._current_owner_reader();e=self.refused(current,'member_authority_unavailable')
   self.assertNotIn('SENTINEL',str(e));self.assertIsNone(e.__cause__)
   self.refused(current,'aggregate_replay')
  self.assertEqual(provider.call_count,2)


if __name__=='__main__':unittest.main(verbosity=2)
