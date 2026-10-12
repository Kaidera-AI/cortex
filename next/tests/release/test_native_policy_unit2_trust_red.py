"""Separately frozen trust controls; public product APIs, inert local fixtures.

Never execute candidate code, launch a compiler, connect to a host or claim
native credit. Native scope below exercises only admission ordering on synthetic
records. Spy ports observe existing controller compile/marshal calls.
"""
import importlib.util
import hashlib
import json
import marshal
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from fixture import canonical, digest, file
import receipt_fixture as producer
from test_unit1_red import product


def verify(case, *, output_reader=None, evidence_reader=None):
    value, raw_inputs, receipt, outputs, evidence, policy = case
    input_api, receipt_api = product('inputs'), product('receipts')
    reader = ObservedInputs(value, raw_inputs) if policy['qualification_scope']=='native' else raw_inputs.__getitem__
    admitted = input_api.admit_build_input(input_api.parse_build_input(canonical(value)), reader, policy)
    parsed = receipt_api.parse_build_receipt(canonical(receipt))
    return receipt_api.verify_build_receipt(parsed, admitted, output_reader or outputs.__getitem__,
                                           evidence_reader or evidence.__getitem__, policy)


class ObservedInputs:
    def __init__(self, value, raw):
        self.value, self.raw = value, raw
    def __call__(self, path):
        return product('inputs').FileObservation(self.raw[path], 'regular', 420)
    def inventory(self, root):
        return [r['path'] for r in self.value['source']['inventory']]


def native_case(*, abi=None):
    """Synthetic native wire scope, NEVER actual native execution evidence."""
    case = list(producer.receipt_fixture(checked_pyc=True))
    value, raw_inputs, receipt, outputs, evidence, policy = case
    old_input = digest(value)
    old_abi = value['builder_requirements']['toolchain']['version']
    if abi: value['builder_requirements']['toolchain']['version'] = abi
    record = value['compatibility_inputs']['compatibility_contract']
    obj = json.loads(raw_inputs[record['path']])
    obj['modules']=[dict(module_id='api',module_version='0.1.020',interface_version=1,schema_version=1,event_version=1)]
    raw_inputs[record['path']]=canonical(obj)
    value['compatibility_inputs']['compatibility_contract']=file(record['path'],raw_inputs[record['path']])
    value['admission_policy']['qualification_scope']='native'
    new_input = digest(value)
    def scope(obj):
        if isinstance(obj,dict): return {k:scope(v) for k,v in obj.items()}
        if isinstance(obj,list): return [scope(v) for v in obj]
        if obj == 'fixture': return 'native'
        if obj == old_input: return new_input
        if abi and obj == old_abi: return abi
        return obj
    receipt = scope(receipt); case[2]=receipt
    for storage in (outputs,evidence):
        for path,raw in list(storage.items()):
            if path.endswith('.json'): storage[path]=canonical(scope(json.loads(raw)))
    def records(obj):
        if isinstance(obj,dict):
            result={k:records(v) for k,v in obj.items()}
            if {'path','bytes','sha256'} <= set(result):
                storage=outputs if result['path'] in outputs else evidence
                if result['path'] in storage:
                    raw=storage[result['path']];result.update(bytes=len(raw),sha256=producer.sha(raw))
            return result
        if isinstance(obj,list):return [records(v) for v in obj]
        return obj
    for _ in range(8):
        for storage in (outputs,evidence):
            for path,raw in list(storage.items()):
                if path.endswith('.json'): storage[path]=canonical(records(json.loads(raw)))
        receipt=records(receipt);case[2]=receipt
    policy.update(input_sha256=new_input,qualification_scope='native',receipt_sha256='f'*64)
    return tuple(case)


def coherent_replacement():
    original_tar=producer.tar
    source=b'fixture_value = 2\n'
    def changed_tar(members):
        if 'app/module.py' in members:
            members['app/module.py']=source
            key=next(k for k in members if k.endswith('.pyc'))
            code=compile(source,'/app/module.py','exec',dont_inherit=True,optimize=0)
            members[key]=importlib.util.MAGIC_NUMBER+(3).to_bytes(4,'little')+importlib.util.source_hash(source)+marshal.dumps(code)
        return original_tar(members)
    with patch.object(producer,'tar',changed_tar): return producer.receipt_fixture(checked_pyc=True)


class TrustControls(unittest.TestCase):
    def test_TRUST001_approved_source_relocation_positive(self):
        case=producer.receipt_fixture(checked_pyc=True)
        self.assertEqual(case[1]['source/module.py'],b'pass\n')
        verified=verify(case)
        self.assertFalse(verified.native_qualified)

    def test_TRUST001_coherent_image_source_and_cache_replacement_refuses(self):
        baseline=producer.receipt_fixture(checked_pyc=True);changed=coherent_replacement()
        self.assertEqual(baseline[0],changed[0]);self.assertEqual(baseline[1],changed[1]);self.assertEqual(baseline[5],changed[5])
        with self.assertRaises(ValueError): verify(changed)

    def test_TRUST001_origin_refusal_precedes_cache_decoder(self):
        case=coherent_replacement();api=product('artifacts')
        with patch.object(api,'_check_pyc',wraps=api._check_pyc) as cache:
            try:verify(case)
            except ValueError:pass
        self.assertEqual(cache.call_count,0,'origin must bind before candidate cache decoding')

    def native_spies(self, *, reject_policy=False):
        case=native_case();api=product('receipts');artifact=product('artifacts')
        value,raw_inputs,receipt,outputs,evidence,policy=case
        admitted=product('inputs').admit_build_input(product('inputs').parse_build_input(canonical(value)),ObservedInputs(value,raw_inputs),policy)
        parsed=api.parse_build_receipt(canonical(receipt))
        if reject_policy: policy['policy_sha256']='a'*64
        output_calls=[];evidence_calls=[]
        def out(path):output_calls.append(path);return outputs[path]
        def ev(path):evidence_calls.append(path);return evidence[path]
        with patch.object(api,'parse_build_receipt',wraps=api.parse_build_receipt) as parser,patch.object(api,'inspect_oci',wraps=api.inspect_oci) as inspector,patch.object(artifact,'_check_pyc',wraps=artifact._check_pyc) as cache:
            try:api.verify_build_receipt(parsed,admitted,out,ev,policy);refused=False
            except ValueError:refused=True
        self.assertTrue(refused,'unaccepted native input must not become verified')
        return dict(receipt_parser=parser.call_count,output_readers=len(output_calls),evidence_readers=len(evidence_calls),oci=inspector.call_count,cache=cache.call_count)

    def test_TRUST002_unaccepted_native_receipt_before_all_readers_and_parsers(self):
        counts=self.native_spies()
        self.assertEqual(counts,dict(receipt_parser=0,output_readers=0,evidence_readers=0,oci=0,cache=0))

    def test_TRUST002_rejected_policy_before_all_readers_and_parsers(self):
        counts=self.native_spies(reject_policy=True)
        self.assertEqual(counts,dict(receipt_parser=0,output_readers=0,evidence_readers=0,oci=0,cache=0))

    def accepted_native(self, **options):
        case=native_case(**options)
        case[5]['receipt_sha256']=digest(case[2])
        return case

    def test_TRUST003_missing_independent_compiler_closure_refuses(self):
        case=self.accepted_native()
        with self.assertRaises(ValueError):verify(case)

    def test_TRUST003_candidate_inspector_context_cannot_authorize_execution(self):
        case=self.accepted_native()
        case[5]['inspector_acceptance']={'origin':'candidate-image','compiler_sha256':'c'*64}
        with self.assertRaises(ValueError):verify(case)

    def test_TRUST003_closure_drift_cannot_authorize_execution(self):
        case=self.accepted_native()
        case[5]['inspector_acceptance']={'origin':'independent','accepted_closure_sha256':'a'*64,'observed_closure_sha256':'b'*64}
        with self.assertRaises(ValueError):verify(case)

    def test_TRUST003_no_controller_compile_or_marshal_before_closure_acceptance(self):
        case=self.accepted_native();api=product('artifacts')
        builtin_compile=compile
        with patch.object(api,'compile',wraps=builtin_compile,create=True) as compiler,patch.object(api.marshal,'load',wraps=api.marshal.load) as decoder:
            try:verify(case)
            except ValueError:pass
        self.assertEqual((compiler.call_count,decoder.call_count),(0,0),'use only independently admitted contained inspector')

    def test_TRUST003_unsupported_abi_before_candidate_readers(self):
        case=self.accepted_native(abi='99.0.0');calls=[]
        def reader(path):calls.append(path);return case[3][path]
        with self.assertRaises(ValueError):verify(case,output_reader=reader)
        self.assertEqual(calls,[],'unsupported ABI must refuse before candidate outputs')

    def test_TRUST003_physical_interpreter_custody_drift_refuses(self):
        case=self.accepted_native()
        with tempfile.TemporaryDirectory(prefix='r01-u2-custody-') as directory:
            member=Path(directory)/'trusted-fixture-interpreter'
            member.write_bytes(b'nonexecuting-fixture-v1');member.chmod(0o600)
            metadata=member.stat()
            accepted={'path':str(member),'sha256':hashlib.sha256(member.read_bytes()).hexdigest(),
                      'device':metadata.st_dev,'inode':metadata.st_ino,'mode':0o600}
            replacement=member.with_name('replacement');replacement.write_bytes(b'nonexecuting-fixture-v2');replacement.chmod(0o600);replacement.replace(member)
            self.assertNotEqual(hashlib.sha256(member.read_bytes()).hexdigest(),accepted['sha256'])
            case[5]['inspector_acceptance']={'origin':'independent-fixture','members':[accepted]}
            with self.assertRaises(ValueError):verify(case)

    def test_TRUST003_nonliteral_inspector_argv_refuses_without_launch(self):
        case=self.accepted_native()
        case[5]['inspector_acceptance']={'origin':'independent-fixture','argv':['sh','-c','synthetic-unexecuted']}
        with self.assertRaises(ValueError):verify(case)

    def test_TRUST003_ambient_startup_hook_never_runs_and_does_not_authorize(self):
        case=self.accepted_native();api=product('artifacts')
        with tempfile.TemporaryDirectory(prefix='r01-u2-startup-') as directory:
            root=Path(directory);marker=root/'hook-ran'
            # Known harmless hook is only written; no interpreter is launched.
            (root/'sitecustomize.py').write_text('from pathlib import Path\nPath('+repr(str(marker))+').write_text("unexpected")\n')
            with patch.dict(os.environ,{'PYTHONPATH':str(root),'PYTHONHOME':str(root)}),patch.object(api,'compile',wraps=compile,create=True) as compiler:
                try:verify(case)
                except ValueError:pass
            self.assertFalse(marker.exists(),'ambient startup hook must never run')
            self.assertEqual(compiler.call_count,0,'no unqualified controller/compiler execution')


if __name__=='__main__':unittest.main(verbosity=2)
