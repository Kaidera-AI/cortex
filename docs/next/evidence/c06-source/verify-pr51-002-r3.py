"""Independent C06 round-three source, classifier, RED and cleanup verifier."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt=Path(sys.argv[1]).resolve()
out=wt/'docs/next/evidence/c06-source'
sha=lambda body:hashlib.sha256(body).hexdigest()
data=json.loads((out/'mutation-pr51-002-r3-full-002.json').read_bytes())
assert data['tree']=='e749432d390860ced54805662f430ece8b108f7e'
assert data['passed'] and data['stack_removed'] and not data['mutation_errors']
assert data['final_inventory_error'] is None
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

clean=[];matrices=[];body=0;copied=None
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
        matrices.append(len(faults))
    if '-c' in command and 'root.rglob' in command[-1] and item.get('exit_code')==0:
        copied=json.loads(item['stdout'])
assert len(clean)==26 and sum(count for _,count in clean)==320
assert ('outbox_inventory',14) in clean
assert matrices==[43,32,30,11,3,1,26,8] and sum(matrices)==154 and body==367
assert copied=={name.removeprefix('next/'):digest for name,digest in source.items()}

new={'test_private_query_match_capture_fails_closed','test_private_query_harmless_statement_fails_closed'}
old=subprocess.check_output(['git','-C',str(wt),'show',
                             '5cad0e1900bf88b9ae1fb84fe7b3fbebd2e639db:next/tests/outbox_inventory/test_outbox_inventory.py'],text=True)
now=(wt/'next/tests/outbox_inventory/test_outbox_inventory.py').read_text()
def methods(text):
    return {node.name:ast.dump(node,include_attributes=False)
            for node in ast.walk(ast.parse(text)) if isinstance(node,ast.FunctionDef) and node.name.startswith('test_')}
original,current=methods(old),methods(now)
assert len(original)==11 and set(current)==set(original)|new
assert all(current[name]==body for name,body in original.items())
for phase,expected in (('red',new),('mutant',new|{
        'test_private_query_walrus_rebind_fails_closed','test_private_query_augassign_rebind_fails_closed'})):
    check=json.loads((out/('pr51-002-r3-'+phase+'.json')).read_text())
    assert check['passed'] and check['source_restored'] and check['resource_operations']==0
    assert check['result']['exit_code']==1 and check['result']['receipt']['tests_run']==14
    assert not check['result']['receipt']['errors']
    failures=check['result']['receipt']['failures']
    assert {row['id'].rsplit('.',1)[-1] for row in failures}==expected
    assert all(row['phase']=='test' and row['is_assertion'] and
               row['phase_attribution']=='traceback+active-unittest-v2' for row in failures)
    assert check['restored']['receipt']=={'tests_run':14,'failures':[],'errors':[]}
    if phase=='mutant':assert check['source_before_sha256']==source['next/scripts/verify_writer_inventory.py']

print(json.dumps({'tested_commit':data['tree'],'raw_sha256':sha((out/'mutation-pr51-002-r3-full-002.json').read_bytes()),
                  'clean_suites':len(clean),'clean_tests':320,'primary_faults':154,
                  'body_assertions':body,'source_files':len(source),'writer_sites':62,
                  'red_body_failures':2,'pin_removal_body_failures':4,
                  'frozen_controls':len(original),'owned_removed':len(removed),'restored':True},indent=2))
