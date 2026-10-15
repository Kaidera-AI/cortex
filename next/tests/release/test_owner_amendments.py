"""Frozen Cox owner amendments; structural/contract tests, no crypto or host."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from jsonschema import Draft202012Validator
NEXT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(NEXT/'src'))
from cortex_core.release.codec import decode_json
FIXTURE=NEXT/'tests/release/r01u3_support/production-policy.shape.json'
SCHEMA=NEXT/'contracts/release/aggregate-policy.schema.json'
PLAN=NEXT/'docs/release/r01u3/ORACLE_CASES.json'
HISTORICAL={
 'rc0_lock_schema':'f7b81910c0bb0de651417054bf46c591916175118ca9eb5fbb849310e5bcf008',
 'rc0_config_schema':'c0f05c8f8ad442dfe781bee08a0eee8926b0d9331e33790c0f2f87c069b78efb',
 'rc0_operations':'32eb25a99a6fdf94f770f5d6b1584ff4b58801686f96a57ad6259788ec8b94a0',
 'migration19_manifest':'08eb769113fd2b5ddbbc65431b8e1da82b077d94f9d4a6faa9e9956f0313edef',
 'event_envelope_v1':'5fa8789b346764a2dc319b0770ca362eff6c76c36d65fb21cd8f2ee46865055a',
}


def policy():return json.loads(FIXTURE.read_bytes())
def schema():return json.loads(SCHEMA.read_bytes())
def plan():return json.loads(PLAN.read_bytes())
def empty():return dict(state='empty',receipt_sha256='e'*64,fence=1,policy_generation=1)
def invalid(value):return list(Draft202012Validator(schema()).iter_errors(value))


class OwnerAmendments(unittest.TestCase):
 def test_explicit_empty_shape_is_possible_without_fake_release(self):
  value=policy();value['installed_baseline']=empty()
  self.assertFalse(invalid(value),'BODY: attested empty branch missing; structural only, not authority')
  self.assertNotIn('release_id',value['installed_baseline'])

 def test_original_installed_branch_unchanged(self):
  self.assertFalse(invalid(policy()))
  branches=schema()['properties']['installed_baseline'].get('oneOf',[])
  self.assertEqual(len(branches),2,'BODY: disjoint installed/empty shape required')
  installed=next((b for b in branches if 'release_id' in b.get('required',[])),None)
  self.assertIsNotNone(installed)
  self.assertEqual(installed['required'],['release_id','receipt_sha256','fence','policy_generation'])
  self.assertEqual(set(installed['properties']),{'release_id','receipt_sha256','fence','policy_generation'})
  self.assertFalse(installed['additionalProperties'])
  self.assertEqual(installed['properties']['fence']['minimum'],0)

 def test_empty_has_no_default_or_installed_fields(self):
  branches=schema()['properties']['installed_baseline'].get('oneOf',[])
  selected=next((b for b in branches if 'state' in b.get('required',[])),None)
  self.assertIsNotNone(selected,'BODY: explicit empty branch not present')
  self.assertEqual(set(selected['required']),{'state','receipt_sha256','fence','policy_generation'})
  self.assertEqual(selected['properties']['state'].get('const'),'empty')
  self.assertFalse(selected['additionalProperties'])
  self.assertNotIn('default',json.dumps(selected))

 def test_empty_float_spelling_still_forbidden_by_sole_raw_codec(self):
  value=policy();value['installed_baseline']=empty();value['installed_baseline']['fence']=1.0
  with self.assertRaises(ValueError):decode_json((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode(),8388608)

 def test_provider_and_refusals_reference_C0_instead_of_copy(self):
  owner=plan().get('cox_owner_amendments',{});ref=owner.get('state_provider',{})
  self.assertEqual(ref.get('owner'),'Finn finite installer V with Cox U13 compatibility')
  self.assertEqual(ref.get('sections'),[1,5,6]);self.assertTrue(ref.get('original_sha256'))
  self.assertTrue(ref.get('reference_only'));self.assertEqual(ref.get('new_reason_vocabulary'),False)
  self.assertEqual(ref.get('source'),'docs/handoffs/2026-10-14_cox_kai_gateb-c0.md')

 def test_requested_current_target_and_replay_order(self):
  order=plan().get('cox_owner_amendments',{}).get('identity_replay',{})
  self.assertEqual(order.get('phases'),['binding','exact_bound_arguments','transport_bounds','requested_raw_id','signature','shape','scope_freshness_sequence','current_target_digest'])
  self.assertTrue(order.get('raw_id_distinct_from_target'))
  self.assertEqual(order.get('stale_authenticated_sequence'),'aggregate_replay')
  self.assertTrue(order.get('crafted_arguments_zero_transport'))

 def test_old_subjects_historical_and_target20_44_GAP(self):
  target=plan().get('cox_owner_amendments',{}).get('compatibility_target',{})
  self.assertEqual(target.get('historical'),HISTORICAL)
  self.assertEqual(target.get('target_label'),'planned20/44')
  self.assertEqual(target.get('target_hashes'),{'final_sql':None,'manifest':None,'lock_schema':None,'descriptor_compatibility':None})
  self.assertEqual(target.get('acceptance_gate'),['final_SQL','manifest','lock_schema','source_review'])
  self.assertEqual(target.get('status'),'GAP_UNRATIFIED_TARGET')
  self.assertTrue(target.get('no_old_hash_as_new_target'));self.assertTrue(target.get('counts_do_not_infer_schema_event_module_versions'))

 def test_four_paired_original_closures_and_existing_classes(self):
  closure=plan().get('cox_owner_amendments',{}).get('per_image_closure',{})
  self.assertEqual(closure.get('images'),[
   {'role':'core-image','module':'core','target':'linux/arm64'}, {'role':'api-image','module':'api','target':'linux/arm64'},
   {'role':'core-image','module':'core','target':'linux/amd64'}, {'role':'api-image','module':'api','target':'linux/amd64'}])
  self.assertEqual(closure.get('paired_receipts_per_image'),1);self.assertEqual(closure.get('independent_slots_per_receipt'),2)
  self.assertEqual(closure.get('record_classes'),['lock-schema','config-schema','operations','descriptor','compatibility','schema','events','migrations','build-input','build-receipt','evidence','sbom','provenance'])
  self.assertEqual(closure.get('required_originals'),['module_input','paired_AB_receipt','evidence_index_and_all_referenced_outputs_audits_custody_cleanup','descriptor_ABI','SBOM_provenance','full_native_package_licence_closure'])
  self.assertEqual(closure.get('external_paired_lock'),1);self.assertFalse(closure.get('new_record_class'))

 def test_capacity_bound_and_secondary_outputs_cannot_disappear(self):
  capacity=plan().get('cox_owner_amendments',{}).get('capacity',{})
  self.assertEqual(capacity.get('caps'),{'manifest':1048576,'signature':4096,'lock':262144,'record':8388608,'image':4294967296,'aggregate':17179869184,'count':10000,'path':4096,'read_seconds':120})
  self.assertTrue(capacity.get('all_referenced_AB_outputs_required'))
  self.assertFalse(capacity.get('receipt_reencoding'));self.assertFalse(capacity.get('silent_cap_raise'))
  self.assertEqual(capacity.get('physical_sizes'),'GAP_NOT_MEASURED')
  self.assertEqual(capacity.get('secondary_output_representation'),'GAP_IF_NOT_EXISTING_CAP_MEDIA_PATH_FIT')
  self.assertTrue(capacity.get('shared_bytes_do_not_merge_distinct_provenance'))

 def test_owner_attestation_and_recheck_cases_remain_NOT_RUN(self):
  owner=plan().get('cox_owner_amendments',{});cases=owner.get('state_cases',[])
  self.assertEqual([r.get('id') for r in cases],['EMPTY_ATTESTED','INSTALLED_AS_EMPTY','UNKNOWN_PROVIDER','NONE_BASELINE','STALE_EMPTY','CONCURRENT_STATE','RECHECK_BEFORE_EFFECT','OLDER_BASELINE_CURRENT_GENERATION'])
  for case in cases:self.assertEqual(case.get('state'),'NOT_RUN_UNBOUND')
  self.assertTrue(owner.get('no_automatic_initial_counters'))
  self.assertTrue(owner.get('baseline_attestation_not_new_host_lease'))
  self.assertTrue(owner.get('B_current_comparison_needs_future_source_alignment'))


def bad_empty(label,change):
 def test(self):
  value=policy();baseline=empty();change(baseline);value['installed_baseline']=baseline
  self.assertTrue(invalid(value),'BODY: empty baseline accepted '+label)
 return test
for label,change in {
 'release_id':lambda b:b.update(release_id='v0.0.0'),
 'zero_receipt':lambda b:b.update(receipt_sha256='0'*64),
 'bad_receipt':lambda b:b.update(receipt_sha256='e'*63),
 'unknown':lambda b:b.update(extra=True),
 'unknown_state':lambda b:b.update(state='unknown'),
 'zero_fence':lambda b:b.update(fence=0),
 'bool_fence':lambda b:b.update(fence=True),
 'zero_generation':lambda b:b.update(policy_generation=0),
 'bool_generation':lambda b:b.update(policy_generation=True),
 'missing_receipt':lambda b:b.pop('receipt_sha256'),
 'missing_fence':lambda b:b.pop('fence'),
 'missing_generation':lambda b:b.pop('policy_generation'),
 'missing_state':lambda b:b.pop('state'),
}.items():setattr(OwnerAmendments,'test_empty_reject_'+label,bad_empty(label,change))

if __name__=='__main__':unittest.main(verbosity=2)
