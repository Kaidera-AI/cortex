"""P1 frozen source oracle. Fixtures/doubles confer no native/install authority."""
import copy
import importlib
import json
from pathlib import Path
import sys
import unittest

from cortex_core.release.codec import decode_json, sha256

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / 'tests/runtime'))
SUBJECTS = json.loads((Path(__file__).parent / 'fixtures/p1-subjects.json').read_bytes())


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()


class ContractOracle(unittest.TestCase):
    def api(self):
        self.assertTrue((NEXT / 'src/cortex_core/install/contracts.py').is_file(), 'P1 surface absent')
        return importlib.import_module('cortex_core.install.contracts')

    def receipt_api(self):
        self.api()
        self.assertTrue((NEXT / 'src/cortex_core/install/receipt.py').is_file(), 'P1 receipt surface absent')
        return importlib.import_module('cortex_core.install.receipt')

    def q(self):
        return copy.deepcopy(SUBJECTS['requests']['linux/arm64/manual'])

    def reject(self, raw, code='bad_shape', kind='request'):
        api = self.api()
        with self.assertRaises(api.InstallRefusal) as caught:
            api.parse_install_message(raw, expected_kind=kind)
        self.assertEqual(caught.exception.reason_code, code)

    def test_all_three_complete_kinds_roundtrip(self):
        api = self.api()
        for name, value in [('request', self.q()), ('receipt', SUBJECTS['receipt']), ('journal_event', SUBJECTS['journal'])]:
            with self.subTest(kind=name):
                parsed = api.parse_install_message(encode(value), expected_kind=name)
                self.assertEqual(parsed.raw, encode(value))
                self.assertEqual(parsed.sha256, sha256(encode(value)))
                with self.assertRaises(TypeError):
                    parsed.document['kind'] = 'changed'

    def test_closed_top_and_nested_fields(self):
        q = self.q()
        for key in q:
            x = copy.deepcopy(q); del x[key]
            with self.subTest(missing=key): self.reject(encode(x))
        for key in ['identity', 'caller', 'target', 'release', 'acquisition', 'selection', 'invocation']:
            x = copy.deepcopy(q); x[key]['unexpected'] = 'refuse'
            with self.subTest(extra=key): self.reject(encode(x))
        x = copy.deepcopy(q); x['unexpected'] = 'refuse'; self.reject(encode(x))

    def test_raw_noncanonical_duplicate_float_bom_unicode_and_limits(self):
        api = self.api(); raw = encode(self.q())
        for candidate in (raw.rstrip(b'\n'), raw+b'\n', b'\xef\xbb\xbf'+raw,
                          raw.replace(b'"format_version":1', b'"format_version":1.0'),
                          raw.replace(b'"format_version":1', b'"format_version":1,"format_version":1'),
                          json.dumps(self.q(), indent=2).encode()+b'\n'):
            with self.subTest(raw=candidate[:20]): self.reject(candidate)
        for value in ['e\u0301', 'bad\x00id']:
            q=self.q();q['identity']['request_id']=value
            self.reject(encode(q))
        with self.assertRaises(api.InstallRefusal): api.parse_install_message(b' '*1048577)

    def test_versions_kinds_boolean_and_string_byte_bounds(self):
        for key,value in [('format_version',True),('format_version',2),('kind','new-kind'),('schema','other')]:
            q=self.q();q[key]=value;self.reject(encode(q))
        q=self.q();q['caller']['owner_uid']=True;self.reject(encode(q))
        q=self.q();q['identity']['request_id']='\u00e9'*65;self.reject(encode(q))
        q=self.q();q['invocation']['argv']=['a']*257;self.reject(encode(q))

    def test_paths_roots_ids_and_manual_parent_rules(self):
        cases=[]
        for value in ['relative','/fixture/../escape','/fixture//alias','/']:
            q=self.q();q['roots'][0]['absolute_path']=value;cases.append(q)
        q=self.q();q['roots'].append(copy.deepcopy(q['roots'][0]));cases.append(q)
        q=self.q();q['caller']['parent_product']='kos';cases.append(q)
        q=self.q();q['caller']['route']='kos_prerequisite';cases.append(q)
        q=self.q();q['release']['installer_ref']['path']='../installer';cases.append(q)
        q=self.q();q['release']['installer_ref']['sha256']='A'*64;cases.append(q)
        for q in cases: self.reject(encode(q))

    def test_termination_variants_and_invalid_sentinels(self):
        api=self.api();r=copy.deepcopy(SUBJECTS['receipt'])
        for outcome,code,signal in [('exited',0,None),('exited',143,None),('signalled',None,15)]:
            x=copy.deepcopy(r);v=x['invocation_result'];v.update(outcome=outcome,exit_code=code,signal=signal,
                executable_witness_ref=x['request_ref'],started_at='2026-10-14T00:00:00Z',ended_at='2026-10-14T00:00:01Z',not_executed_reason=None)
            x.update(result='FAIL',reason_code='execution_failed',phase='finished',journal_ref=x['request_ref'])
            api.parse_install_message(encode(x),expected_kind='receipt')
        for outcome,code,signal in [('exited',None,None),('exited',0,15),('exited',True,None),('exited',256,None),('signalled',143,15),('signalled',None,0),('signalled',None,-15),('not_executed',0,None)]:
            x=copy.deepcopy(r);x['invocation_result'].update(outcome=outcome,exit_code=code,signal=signal)
            self.reject(encode(x),kind='receipt')
        x=copy.deepcopy(r);x['invocation_result']['started_at']='2026-10-14T00:00:00Z';self.reject(encode(x),kind='receipt')

    def test_original_ref_digest_size_and_real_build_A_filename(self):
        api=self.api();raw=SUBJECTS['real_build_A'].encode()
        api.measure_original_ref(SUBJECTS['real_build_A_ref'],raw)
        legacy=decode_json(raw,1048576,canonical=False)
        self.assertEqual(len(raw),3224)
        self.assertEqual(sha256(raw),'7c96304cfe47681904ebd84e4dbae7e2a0c28e4eea63f60f5e7a6e672f9111b4')
        self.assertNotIn('pkg',legacy['outputs'])
        self.assertEqual(legacy['outputs'][SUBJECTS['real_build_A_output_key']],SUBJECTS['real_build_A_output'])
        for field,value in [('sha256','0'*64),('bytes',3225)]:
            ref=copy.deepcopy(SUBJECTS['real_build_A_ref']);ref[field]=value
            with self.assertRaises(api.InstallRefusal):api.measure_original_ref(ref,raw)
        with self.assertRaises(api.InstallRefusal):api.measure_original_ref(SUBJECTS['real_build_A_ref'],raw+b' ')

    def test_journal_sequence_and_original_identity(self):
        api=self.api();q=self.q();event=copy.deepcopy(SUBJECTS['journal'])
        validated=api.validate_journal([encode(event)],encode(q))
        self.assertEqual(len(validated),1)
        for field,value in [('sequence',1),('previous_event_ref',event['request_ref'])]:
            x=copy.deepcopy(event);x[field]=value
            with self.assertRaises(api.InstallRefusal):api.validate_journal([encode(x)],encode(q))
        x=copy.deepcopy(event);x['identity']['transaction_id']='foreign'
        with self.assertRaises(api.InstallRefusal):api.validate_journal([encode(x)],encode(q))

    def test_receipt_binds_original_request_not_its_self_hash(self):
        api=self.receipt_api();q=self.q();r=copy.deepcopy(SUBJECTS['receipt'])
        parsed=api.verify_product_receipt(encode(r),encode(q),request_ref=r['request_ref'])
        self.assertEqual(parsed.document['result'],'PASS')
        for field,value in [('profile','foreign'),('journal_ref',r['request_ref'])]:
            x=copy.deepcopy(r);x[field]=value
            with self.assertRaises(self.api().InstallRefusal):api.verify_product_receipt(encode(x),encode(q),request_ref=r['request_ref'])
        x=copy.deepcopy(r);x['request_ref']['sha256']='0'*64
        with self.assertRaises(self.api().InstallRefusal):api.verify_product_receipt(encode(x),encode(q),request_ref=r['request_ref'])

    def test_prior_observation_null_journal_requires_exact_evidence(self):
        api=self.receipt_api();q=self.q();q['previous_receipt_ref']=SUBJECTS['receipt']['request_ref'];q['journal_ref']=SUBJECTS['receipt']['request_ref']
        r=copy.deepcopy(SUBJECTS['receipt']);r['request_ref']={'path':'request.json','bytes':len(encode(q)),'sha256':sha256(encode(q))}
        r['disposition']='verified_owned_continuation';r['evidence_refs']=[q['previous_receipt_ref']]
        api.verify_product_receipt(encode(r),encode(q),request_ref=r['request_ref'])
        r['evidence_refs']=[]
        with self.assertRaises(self.api().InstallRefusal):api.verify_product_receipt(encode(r),encode(q),request_ref=r['request_ref'])
        r['evidence_refs']=[q['previous_receipt_ref']];r['journal_ref']=q['journal_ref']
        with self.assertRaises(self.api().InstallRefusal):api.verify_product_receipt(encode(r),encode(q),request_ref=r['request_ref'])

    def test_public_export_gate_and_schema_identity(self):
        self.api();pkg=importlib.import_module('cortex_core.install')
        for name in ['parse_install_message','admit_install_request','verify_product_receipt','validate_journal']:
            self.assertTrue(callable(getattr(pkg,name,None)),name)
        schema=json.loads((NEXT/'contracts/install/operation.schema.json').read_bytes())
        self.assertEqual(schema['$id'],'urn:kaidera:install-operation:v1')
        self.assertEqual(len(schema['oneOf']),3)
        policy=json.loads((NEXT/'release/install/shared-distribution-policy.v1.json').read_bytes())
        self.assertIsNone(policy['implementation_manifest_ref'])
        self.assertIn('cortex_core.runtime.signed_member',policy['required_imports'])


    def test_single_schema_unknown_field_decides_in_body(self):
        q=self.q();q['unexpected']='reject';self.reject(encode(q))

    def test_complete_executed_tuple_deciding_signal_and_time_negatives(self):
        r=copy.deepcopy(SUBJECTS['receipt']);v=r['invocation_result']
        v.update(outcome='exited',exit_code=0,signal=None,executable_witness_ref=r['request_ref'],
                 started_at='2026-10-14T00:00:01Z',ended_at='2026-10-14T00:00:02Z',not_executed_reason=None)
        r.update(result='FAIL',reason_code='execution_failed',phase='finished',journal_ref=r['request_ref'])
        self.api().parse_install_message(encode(r),expected_kind='receipt')
        for field,value in [('signal',15),('executable_witness_ref',None),('ended_at','2026-10-14T00:00:00Z')]:
            x=copy.deepcopy(r);x['invocation_result'][field]=value
            self.reject(encode(x),kind='receipt')

    def test_ready_requires_actual_bound_descriptor_connection_checks_and_state(self):
        r=copy.deepcopy(SUBJECTS['receipt']);r['readiness']['state']='ready'
        self.reject(encode(r),kind='receipt')

    def test_request_specific_size_bound_with_otherwise_complete_canonical_shape(self):
        q=self.q();q['acquisition']['local_members']=[{'path':('x'*100)+str(i),'bytes':0,'sha256':'0'*64} for i in range(7000)]
        raw=encode(q);self.assertGreater(len(raw),1048576);self.assertLess(len(raw),8388608)
        self.reject(raw)

    def test_original_ref_rejects_non_json_digest_equality_objects(self):
        api=self.api()
        class AlwaysEqual:
            def __eq__(self,other):return True
        raw=b'public original';record={'path':'record.json','bytes':len(raw),'sha256':AlwaysEqual()}
        with self.assertRaises(api.InstallRefusal):api.measure_original_ref(record,raw)

    def test_receipt_cross_identity_decides_in_body(self):
        r=copy.deepcopy(SUBJECTS['receipt']);r['identity']['transaction_id']='foreign'
        with self.assertRaises(self.api().InstallRefusal):
            self.receipt_api().verify_product_receipt(encode(r),encode(self.q()),request_ref=SUBJECTS['receipt']['request_ref'])


if __name__=='__main__':unittest.main()
