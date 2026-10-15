"""Frozen correction controls: case contract and test-recipe observations only.

No key, signature, provider import or crypto execution. Protocol doubles are
NON-AUTHORITATIVE and earn no verifier/native credit. Original oracle unchanged.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

NEXT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(NEXT/'src'),str(NEXT/'tests/release')]
from cortex_core.runtime.signed_member import Refusal

PLAN_PATH=NEXT/'docs/release/r01u3/ORACLE_CASES.json'
POLICY_PATH=NEXT/'contracts/release/aggregate-policy.schema.json'
# Every number is an independently observed call count in future actual tests.
# This test freezes the recipe, never grants trust to a caller's receipt/flag.
RECIPES={
 'SIG02':('aggregate_root_mismatch','root_pin',0,0,'root_material_sha256'),
 'SIG02-ID':('aggregate_root_mismatch','packet_key_id',0,0,'packet_key_id'),
 'SIG07-FIRST':('aggregate_signature_invalid','first_crypto',1,0,'first_signature'),
 'SIG07-GLOBAL':('aggregate_signature_invalid','global_crypto',1,1,'global_signature'),
 'SIG08-REQUEST-ID':('aggregate_identity_mismatch','requested_raw_id',0,0,'requested_raw_id'),
 'SIG08-ORIGINAL':('aggregate_signature_invalid','first_crypto',1,0,'first_signature_original_bytes'),
 'SIG08-REENCODING':('aggregate_signature_invalid','first_crypto',1,0,'first_signature_reencoding'),
 'SIG10-TRANSPORT':('member_path_alias','aggregate_transport',0,0,'owner_locator_lexical'),
 'SIG10-SIGNED-SHAPE':('aggregate_shape','manifest_shape',1,1,'signed_member_path_grammar'),
 'SIG10-PHYSICAL':('member_path_alias','member_transport',1,1,'member_physical_alias'),
 'SIG11-AUTHENTIC-CONTEXT':('aggregate_scope_mismatch','trusted_context',1,1,'trusted_comment_grammar'),
 'SIG14':('aggregate_shape','manifest_shape',1,1,'KOS_schema'),
 'SIG14-DOMAIN':('aggregate_scope_mismatch','trusted_context',1,1,'KOS_comment_on_Cortex_shape'),
 'CURRENT-HASH':('aggregate_identity_mismatch','current_digest',1,1,'owner_current_manifest_hash'),
}


def load_plan():return json.loads(PLAN_PATH.read_bytes())


def assert_deciding_recipe(test,invoke,observe,*,reason,first,second):
 """Assertions reused by an actual test's independently instrumented adapter.

 invoke runs the actual target. observe reads test-owned spies, not target data.
 A synthetic observer here exercises this ASSERTION CONTRACT only. It does not
 stand in for the mandatory actual provider/body/transport/effect spies later.
 """
 returned=None;error=None
 try:returned=invoke()
 except Exception as caught:error=caught
 test.assertIsNone(returned,'BODY: no admitted/partial result after refusal')
 test.assertIsInstance(error,Refusal,'BODY: required typed refusal; a callable surface or None is not evidence')
 test.assertEqual(error.reason,reason,'BODY: exact earliest deciding refusal')
 counters=observe()
 for key,expected in [('first_crypto',first),('global_crypto',second),('protected_delivery',0),('effect_grant',0),('primitive_effect',0)]:
  test.assertIs(type(counters.get(key)),int,'BODY: independent exact counter '+key)
  test.assertEqual(counters[key],expected,'BODY: independent deciding counter '+key)


class PlanCorrections(unittest.TestCase):
 def test_all_recipes_bound_to_actual_observation_protocol(self):
  plan=load_plan();contract=plan.get('deciding_body_contract',{})
  self.assertEqual(contract.get('observer_owner'),'test-owned wrappers around actual production bodies, outside returned result')
  self.assertTrue(contract.get('original_assertions_frozen'))
  self.assertEqual(contract.get('positive_then_guard_omission_then_exact_restore'),True)
  self.assertEqual(contract.get('surface_setup_mock_receipt_credit'),False)

 def test_original_crypto_and_native_classifications_honest(self):
  plan=load_plan()
  self.assertEqual(plan.get('crypto_keys_created'),0);self.assertEqual(plan.get('signatures_created'),0)
  self.assertEqual(plan.get('classification'),'FROZEN_DESIRED_CASES_NOT_EXECUTION')
  self.assertTrue(plan.get('tightened_recipes_require_new_execution_receipt'))
  self.assertTrue(plan.get('u3b_evidence_reconciliation'))
  for case in plan['cases']:self.assertEqual(case['state'],'NOT_RUN_UNBOUND')

 def test_policy_compatibility_definition_unreachable_and_deleted(self):
  schema=json.loads(POLICY_PATH.read_bytes());refs=[]
  def walk(value):
   if isinstance(value,dict):
    if '$ref' in value:refs.append(value['$ref'])
    for item in value.values():walk(item)
   elif isinstance(value,list):
    for item in value:walk(item)
  walk(schema)
  self.assertNotIn('#/$defs/compatibility',refs,'BODY: deletion must be reference-safe')
  self.assertNotIn('compatibility',schema['$defs'],'BODY: unreachable policy definition not deleted')

 def test_original_coarse_case_ids_redirect_to_explicit_variants(self):
  cases={c['id']:c for c in load_plan()['cases']}
  self.assertEqual(cases['SIG08'].get('variants'),['SIG08-REQUEST-ID','SIG08-ORIGINAL','SIG08-REENCODING'])
  self.assertEqual(cases['SIG10'].get('variants'),['SIG10-TRANSPORT','SIG10-SIGNED-SHAPE','SIG10-PHYSICAL'])
  self.assertEqual(cases['SIG07'].get('expected'),'aggregate_signature_invalid')
  self.assertEqual(cases['SIG14'].get('expected'),'aggregate_shape')

 def test_multifault_earliest_refusal_matrix(self):
  plan=load_plan();matrix=plan.get('multi_fault_precedence',[])
  actual={r.get('id'):(r.get('expected'),r.get('first_crypto'),r.get('global_crypto')) for r in matrix}
  self.assertEqual(actual,{
   'authority_before_all':('required_binding_gap',0,0),
   'argument_before_transport_crypto':('member_identity_mismatch',0,0),
   'requested_id_before_crypto':('aggregate_identity_mismatch',0,0),
   'envelope_before_scope':('aggregate_signature_invalid',0,0),
   'root_before_scope':('aggregate_root_mismatch',0,0),
   'shape_before_scope':('aggregate_shape',1,1),
   'older_sequence_before_current_hash':('aggregate_replay',1,1),
  },'BODY: one earliest refusal for each explicit combined input')

 def test_recipe_assertions_accept_matched_synthetic_protocol_only(self):
  counters={'first_crypto':1,'global_crypto':0,'protected_delivery':0,'effect_grant':0,'primitive_effect':0}
  def non_authoritative_protocol_double():raise Refusal('aggregate_signature_invalid')
  assert_deciding_recipe(self,non_authoritative_protocol_double,lambda:counters,reason='aggregate_signature_invalid',first=1,second=0)

 def test_non_verifying_none_dummy_is_rejected_by_original_recipe_assertion(self):
  counters={'first_crypto':0,'global_crypto':0,'protected_delivery':0,'effect_grant':0,'primitive_effect':0};calls=[]
  def dummy():calls.append(1);return None
  with self.assertRaises(AssertionError):assert_deciding_recipe(self,dummy,lambda:counters,reason='aggregate_signature_invalid',first=1,second=0)
  self.assertEqual(calls,[1])

 def test_refusal_only_dummy_without_real_deciding_call_is_rejected(self):
  counters={'first_crypto':0,'global_crypto':0,'protected_delivery':0,'effect_grant':0,'primitive_effect':0}
  def refusal_only():raise Refusal('aggregate_signature_invalid')
  with self.assertRaises(AssertionError):assert_deciding_recipe(self,refusal_only,lambda:counters,reason='aggregate_signature_invalid',first=1,second=0)


def recipe_case(case_id,expected):
 def test(self):
  plan=load_plan();case=next((c for c in plan['cases'] if c['id']==case_id),None)
  self.assertIsNotNone(case,'BODY: missing isolated recipe '+case_id)
  reason,phase,first,second,only_fault=expected
  self.assertEqual(case.get('expected'),reason,'BODY: exact earliest refusal '+case_id)
  self.assertEqual(case.get('deciding_phase'),phase,'BODY: one deciding phase '+case_id)
  self.assertEqual(case.get('first_crypto'),first,'BODY: first verification observation '+case_id)
  self.assertEqual(case.get('global_crypto'),second,'BODY: global verification observation '+case_id)
  self.assertEqual(case.get('fault'),only_fault,'BODY: isolated subject '+case_id)
  self.assertTrue(case.get('all_earlier_phases_valid'),'BODY: earlier stage preempted '+case_id)
  self.assertEqual(case.get('protected_delivery'),0);self.assertEqual(case.get('effect_grant'),0);self.assertEqual(case.get('primitive_effect'),0)
  self.assertTrue(case.get('input_recipe'),'BODY: exact construction missing '+case_id)
  self.assertTrue(case.get('causal_omission'),'BODY: deciding guard omission missing '+case_id)
  self.assertTrue(case.get('matched_positive_and_restore'),'BODY: positive/restoration absent '+case_id)
  self.assertIn(case.get('coverage'),['B_EXISTING_PARTIAL_REUSE','FUTURE_NOT_RUN'])
 return test

for case_id,expected in RECIPES.items():setattr(PlanCorrections,'test_recipe_'+case_id.replace('-','_'),recipe_case(case_id,expected))

if __name__=='__main__':unittest.main(verbosity=2)
