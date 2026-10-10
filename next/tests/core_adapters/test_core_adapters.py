"""Executable private-port/legacy-map boundaries and mutation caller guards."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from core_db_fixture import Fixture,OWNER_A,WRITE_A,uid
from cortex_core import coordination,records
try:
    import mutate_core_adapters as consumer
except ImportError:
    consumer=None
NEXT=Path(__file__).resolve().parents[2]

class AdapterConformance(Fixture):
    def fixture(self):return json.loads((NEXT/'contracts/core-adapters-conformance.json').read_text())
    def test_fixture_request_hits_real_canonical_record_port(self):
        case=self.fixture()['record_case'];body=bytes.fromhex(case['body_hex'])
        a=records.Records(self.request,WRITE_A,UUID(uid(1)),UUID(uid(3)))
        r=a.put(UUID(uid(80)),case['kind'],body,case['expected_revision'],case['request_key'])
        self.assertEqual((r.revision,r.payload_sha256),(1,hashlib.sha256(b'exact\x00\xff').hexdigest()))
        self.assertEqual(a.get(r.record_id).body,b'exact\x00\xff')
    def test_legacy_method_aliases_are_retained_with_real_private_targets(self):
        f=self.fixture();spec=json.loads((NEXT/'contracts/openapi.json').read_text())
        operations={v['operationId'] for path in spec['paths'].values() for v in path.values() if isinstance(v,dict) and 'operationId' in v}
        self.assertIn(f['record_write_operation'],operations)
        self.assertEqual(len(f['handoff_aliases']),14)
        for operation,method in f['handoff_aliases'].items():
            self.assertIn(operation,operations);self.assertTrue(callable(getattr(coordination.Jobs,method,None)))
        self.assertEqual(f['handoff_aliases']['C01-R055_put_complete_handoff'],'accept')
        self.assertEqual(f['release_semantics'],'unresolved-unproven-core; exact legacy release held')
    def test_external_ack_and_role_human_compatibility_remain_explicitly_held(self):
        f=self.fixture();self.assertFalse(f['canonical_event_ack_qualified']);self.assertFalse(f['role_human_handoff_qualified'])
        self.assertEqual(set(f['held_gates']),{'C04a-auth-0003','C02-released-pins','C06-capture','C11-http'})
        self.assertNotIn('event_id',records.MutationReceipt.__dataclass_fields__)
    def test_budget_fixture_hits_real_claim_without_enforcement(self):
        f=self.fixture();a=coordination.Jobs(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        a.create(UUID(uid(80)),'extract',b'intent','create')
        r=a.claim_with_budget(UUID(uid(80)),'claim',budget=0)
        self.assertEqual(r.budget_status,f['budget_status']);self.assertEqual(r.budget_status,'not_enforced')
    def receipt(self,exit_code,failures=(),errors=()):
        return SimpleNamespace(returncode=exit_code,stdout='CORTEX_TEST_RESULT='+json.dumps({'tests_run':1,'failures':list(failures),'errors':list(errors)}),stderr='')
    def test_mutation_caller_refuses_operational_fixture_wrong_test_and_errors(self):
        self.assertIsNotNone(consumer,'C05 mutation caller is missing')
        body={'id':'expected','traceback':'actual evidence','phase':'test','is_assertion':True}
        for result in (self.receipt(137,[body]),self.receipt(1,[dict(body,phase='fixture')]),self.receipt(1,[dict(body,id='wrong')]),self.receipt(1,[body],[dict(id='error',traceback='error')])):
            self.assertEqual(consumer.mutation_status(result,{'expected'}),'inconclusive')
    def test_mutation_caller_requires_expected_body_and_zero_error_clean_baseline(self):
        self.assertIsNotNone(consumer,'C05 mutation caller is missing')
        body={'id':'expected','traceback':'actual evidence','phase':'test','is_assertion':True}
        self.assertEqual(consumer.mutation_status(self.receipt(1,[body]),{'expected'}),'killed')
        self.assertEqual(consumer.mutation_status(self.receipt(0),set()),'survived')
