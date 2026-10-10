"""Independent C06 r4 source, scanner, fault and cleanup receipt check."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt=Path(sys.argv[1]).resolve()
out=wt/'docs/next/evidence/c06-source'
sha=lambda body:hashlib.sha256(body).hexdigest()
raw=out/'mutation-pr51-002-r4-full-002.json'
data=json.loads(raw.read_bytes())
tested='3a7bc93932b7bfa80083a4ff064afdea33b88900'
assert data['tree']==tested and data['passed'] and data['stack_removed']
assert not data['mutation_errors'] and data['final_inventory_error'] is None
cleanup=data['cleanup']
assert cleanup['cleanup_verified'] and cleanup['password_discarded'] and cleanup['lock_released']
assert not cleanup['pending'] and not cleanup['owned']
created={(x['kind'],x['id']) for x in cleanup['events'] if x['event']=='created'}
removed={(x['kind'],x['id']) for x in cleanup['events'] if x['event']=='removed'}
assert len(created)==3 and created==removed
assert data['native_arch']=='arm64' and not data['published_ports'] and not data['bind_mounts']
assert data['limits']=={'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'}
source=data['source_sha256']
assert len(source)==426
for path,digest in source.items():
    assert sha((wt/path).read_bytes())==digest,path
    assert sha(subprocess.check_output(['git','-C',str(wt),'show',data['tree']+':'+path]))==digest,path
fixed=subprocess.check_output(['git','-C',str(wt),'show',
                               'fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f:next/tests/test_receipts.py'])
assert sha(fixed)==source['next/tests/test_receipts.py']
wheels=json.loads((out/'offline-wheel-inputs.json').read_text())['wheels']
assert data['tool_input_sha256']['actual_wheels']==wheels==data['tool_input_sha256']['copied_wheels']

clean=[];matrices=[];body=0;copied=None;inventory_faults={}
for item in data['results']:
    command=item['command'];lines=item.get('stdout','').splitlines()
    if '/tmp/next/tests/test_receipts.py' in command and item.get('exit_code')==0:
        reports=[json.loads(line.split('=',1)[1]) for line in lines if line.startswith('CORTEX_TEST_RESULT=')]
        if reports:
            assert not reports[-1]['failures'] and not reports[-1]['errors']
            clean.append((Path(command[-1]).name,reports[-1]['tests_run']))
    if any('/tmp/next/scripts/mutate_' in part for part in command):
        assert item['exit_code']==0
        rows=[json.loads(line) for line in lines if line.startswith('{')]
        faults=[row for row in rows if 'status' in row]
        summary=rows[-1]
        assert len(faults)==summary['mutants']==summary['killed']
        assert not summary['survivors'] and not summary['inconclusive']
        for fault in faults:
            receipt=fault['receipt']
            assert fault['status']=='killed' and fault['exit_code']!=0
            assert not receipt['errors'] and receipt['failures']
            assert fault['expected_test'] in {f['id'].split(' (')[0] for f in receipt['failures']}
            assert all(f['phase']=='test' and f['is_assertion'] and
                       f['phase_attribution']=='traceback+active-unittest-v2' for f in receipt['failures'])
            body+=len(receipt['failures'])
            if fault['mutation'] in ('inventory checker admits unknown writer',
                                     'quoted and unqualified writer forms ignored'):
                inventory_faults[fault['mutation']]=fault
        matrices.append(len(faults))
    if '-c' in command and 'root.rglob' in command[-1] and item.get('exit_code')==0:
        copied=json.loads(item['stdout'])
assert len(clean)==26 and sum(count for _,count in clean)==323
assert ('outbox_inventory',17) in clean
assert matrices==[43,32,30,11,3,1,26,8] and sum(matrices)==154
assert body==372
assert set(inventory_faults)=={'inventory checker admits unknown writer',
                              'quoted and unqualified writer forms ignored'}
assert all(fault['auxiliary_control']['qualified'] and
           fault['auxiliary_control']['expected_boundary_unchanged']
           for fault in inventory_faults.values())
assert copied=={name.removeprefix('next/'):digest for name,digest in source.items()}

def methods(text):
    return {node.name:ast.dump(node,include_attributes=False)
            for node in ast.walk(ast.parse(text)) if isinstance(node,ast.FunctionDef) and node.name.startswith('test_')}
old=subprocess.check_output(['git','-C',str(wt),'show',
    'c7dff73eec7f0fce16dc2cf6a80f969e58d93e77:next/tests/outbox_inventory/test_outbox_inventory.py'],text=True)
now=(wt/'next/tests/outbox_inventory/test_outbox_inventory.py').read_text()
original,current=methods(old),methods(now)
new={'test_private_alias_caller_fails_closed','test_cross_file_private_import_alias_fails_closed',
     'test_dynamic_name_primitives_fail_closed'}
assert len(original)==13 and set(current)==set(original)|new
assert all(current[name]==value for name,value in original.items())
scanner=source['next/scripts/verify_writer_inventory.py']
for label in ('py312','py314'):
    red=json.loads((out/('pr51-002-r4-red-'+label+'.json')).read_text())
    green=json.loads((out/('pr51-002-r4-green-'+label+'.json')).read_text())
    mutant=json.loads((out/('pr51-002-r4-mutants-'+label+'.json')).read_text())
    assert red['exit_code']==1 and red['receipt']['tests_run']==17
    assert len(red['receipt']['failures'])==8 and not red['receipt']['errors']
    assert all(f['phase']=='test' and f['is_assertion'] for f in red['receipt']['failures'])
    assert green['scanner_sha256']==scanner and green['exit_code']==0
    assert green['receipt']=={'tests_run':17,'failures':[],'errors':[]}
    assert green['writer_inventory']=={'passed':True,'writer_count':62,
        'unclassified':[],'missing':[],'stale':[]}
    assert mutant['passed'] and mutant['source_restored'] and mutant['resource_operations']==0
    assert mutant['source_before_sha256']==scanner
    assert [(row['guard'],len(row['result']['receipt']['failures'])) for row in mutant['mutants']]==[
        ('whole_file_pin',1),('cross_file_reference',1),('dynamic_name',6)]
    assert mutant['restored']==mutant['baseline']=={'exit_code':0,
        'receipt':{'tests_run':17,'failures':[],'errors':[]}}
audit=json.loads((out/'known-input-reference-audit.json').read_text())
assert audit['scanner_sha256']==scanner and audit['files_scanned']==20
assert not audit['cross_file_exempt_reference_hits'] and not audit['nonliteral_name_hits']
assert audit['current_unclassified_source_flags']==0
assert all(not row['current_hits'] for row in audit['primitive_dispositions'].values())
print(json.dumps({'tested_commit':data['tree'],'raw_sha256':sha(raw.read_bytes()),
                  'clean_suites':len(clean),'clean_tests':323,'primary_faults':154,
                  'body_assertions':body,'source_files':len(source),'writer_sites':62,
                  'red_body_failures_each_python':8,'new_guard_mutants_each_python':[1,1,6],
                  'previous_inventory_methods_preserved':len(original),
                  'current_dynamic_name_hits':0,'owned_removed':len(removed),'restored':True},indent=2))
