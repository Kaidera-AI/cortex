"""Independent C06-PR51-002-r2 receipt, source and cleanup check."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
out = wt/'docs/next/evidence/c06-source'
sha = lambda body: hashlib.sha256(body).hexdigest()
raw = out/'mutation-pr51-002-r2-full-001.json'
data = json.loads(raw.read_bytes())
assert data['tree'] == '6e8d22de80dbb994d049034a355b49aa5959a897'
assert data['passed'] and data['stack_removed'] and not data['mutation_errors']
assert data['final_inventory_error'] is None
cleanup = data['cleanup']
assert cleanup['cleanup_verified'] and cleanup['password_discarded'] and cleanup['lock_released']
assert not cleanup['pending'] and not cleanup['owned']
created = {(e['kind'],e['id']) for e in cleanup['events'] if e['event']=='created'}
removed = {(e['kind'],e['id']) for e in cleanup['events'] if e['event']=='removed'}
assert len(created)==3 and created==removed
assert data['native_arch']=='arm64' and not data['published_ports'] and not data['bind_mounts']
assert data['limits']=={'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'}
source = data['source_sha256']
assert len(source)==426
for path,digest in source.items():
    assert sha((wt/path).read_bytes())==digest,path
    assert sha(subprocess.check_output(['git','-C',str(wt),'show',data['tree']+':'+path]))==digest,path
fixed = subprocess.check_output(['git','-C',str(wt),'show','fdd04d7b8b8f977e3c3b94f7badc9a17c87d787f:next/tests/test_receipts.py'])
assert sha(fixed)==source['next/tests/test_receipts.py']
expected_wheels=json.loads((out/'offline-wheel-inputs.json').read_text())['wheels']
assert data['tool_input_sha256']['actual_wheels']==expected_wheels==data['tool_input_sha256']['copied_wheels']
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
        mutants=[row for row in rows if 'status' in row]
        summary=rows[-1]
        assert len(mutants)==summary['mutants']==summary['killed']
        assert not summary['survivors'] and not summary['inconclusive']
        for mutant in mutants:
            receipt=mutant['receipt']
            assert mutant['status']=='killed' and mutant['exit_code']!=0
            assert not receipt['errors'] and receipt['failures']
            assert mutant['expected_test'] in {f['id'].split(' (')[0] for f in receipt['failures']}
            assert all(f['phase']=='test' and f['is_assertion'] and
                       f['phase_attribution']=='traceback+active-unittest-v2' for f in receipt['failures'])
            body += len(receipt['failures'])
        matrices.append(len(mutants))
    if '-c' in command and 'root.rglob' in command[-1] and item.get('exit_code')==0:
        copied=json.loads(item['stdout'])
assert len(clean)==26 and sum(count for _,count in clean)==318
assert ('outbox_inventory',12) in clean
assert matrices==[43,32,30,11,3,1,26,8] and sum(matrices)==154 and body==367
assert copied=={name.removeprefix('next/'):digest for name,digest in source.items()}
red=json.loads((out/'pr51-002-r2-red.json').read_text())
green=json.loads((out/'pr51-002-r2-green.json').read_text())
mutant=json.loads((out/'pr51-002-r2-mutant.json').read_text())
expected={'test_outbox_inventory.WriterInventory.test_private_query_walrus_rebind_fails_closed',
          'test_outbox_inventory.WriterInventory.test_private_query_augassign_rebind_fails_closed'}
assert red['receipt']['tests_run']==12 and red['exit_code']==1 and not red['receipt']['errors']
assert {f['id'] for f in red['receipt']['failures']}==expected
assert all(f['phase']=='test' and f['is_assertion'] for f in red['receipt']['failures'])
assert green['suite_exit_code']==0 and green['receipt']=={'tests_run':12,'failures':[],'errors':[]}
assert mutant['passed'] and mutant['source_restored'] and mutant['resource_operations']==0
assert mutant['source_before_sha256']==source['next/scripts/verify_writer_inventory.py']
assert {f['id'] for f in mutant['mutant']['receipt']['failures']}==expected
assert not mutant['mutant']['receipt']['errors']
assert all(f['phase']=='test' and f['is_assertion'] and
           f['phase_attribution']=='traceback+active-unittest-v2' for f in mutant['mutant']['receipt']['failures'])
print(json.dumps({'tested_commit':data['tree'],'raw_sha256':sha(raw.read_bytes()),
                  'clean_suites':len(clean),'clean_tests':318,'primary_faults':154,
                  'body_assertions':body,'source_files':len(source),'writer_sites':62,
                  'red_tests':2,'guard_removal_body_failures':len(mutant['mutant']['receipt']['failures']),
                  'classifier_sha256':sha(fixed),'owned_removed':len(removed),'restored':True},indent=2))
