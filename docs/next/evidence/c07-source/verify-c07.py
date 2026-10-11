"""Independent C07 native source, old-guard, fault and cleanup verifier."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt=Path(sys.argv[1]).resolve()
out=wt/'docs/next/evidence/c07-source'
sha=lambda body:hashlib.sha256(body).hexdigest()
raw=out/'mutation-c07-final-004.json'
data=json.loads(raw.read_bytes())
tested='b7b4f854ec0862ed1c1626e5be29c36ea17a9bfb'
assert data['tree']==tested and data['passed'] and data['stack_removed']
assert not data['mutation_errors'] and data['final_inventory_error'] is None
assert subprocess.run(['git','-C',str(wt),'merge-base','--is-ancestor',
                       'c7dff73eec7f0fce16dc2cf6a80f969e58d93e77',tested]).returncode==0
cleanup=data['cleanup']
assert cleanup['cleanup_verified'] and cleanup['password_discarded'] and cleanup['lock_released']
assert not cleanup['pending'] and not cleanup['owned']
created={(x['kind'],x['id']) for x in cleanup['events'] if x['event']=='created'}
removed={(x['kind'],x['id']) for x in cleanup['events'] if x['event']=='removed'}
assert len(created)==3 and created==removed
assert data['native_arch']=='arm64' and not data['published_ports'] and not data['bind_mounts']
assert data['limits']=={'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'}
source=data['source_sha256']
assert len(source)==439
for path,digest in source.items():
    assert sha((wt/path).read_bytes())==digest,path
    assert sha(subprocess.check_output(['git','-C',str(wt),'show',tested+':'+path]))==digest,path
assert sha((out/'run-core-pg.py').read_bytes())==data['controller_sha256']
assert sha(subprocess.check_output(['git','-C',str(wt),'show',tested+':docs/next/evidence/c07-source/run-core-pg.py']))==data['controller_sha256']
for name in ('run-old-table-guards.py','frozen/auth-test_authorization.py',
             'frozen/auth_identity-test_identity.py','frozen/outbox-test_outbox.py'):
    path=out/name
    assert sha(path.read_bytes())==data['tool_input_sha256'][name]
    assert sha(subprocess.check_output(['git','-C',str(wt),'show',
                                       tested+':docs/next/evidence/c07-source/'+name]))==data['tool_input_sha256'][name]
wheels=json.loads((wt/'docs/next/evidence/c06-source/offline-wheel-inputs.json').read_text())['wheels']
assert data['tool_input_sha256']['actual_wheels']==wheels==data['tool_input_sha256']['copied_wheels']
manifest=json.loads((wt/'next/schema/manifest.json').read_text())['migrations']
prior=json.loads(subprocess.check_output(['git','-C',str(wt),'show',
    'c7dff73eec7f0fce16dc2cf6a80f969e58d93e77:next/schema/manifest.json']))['migrations']
assert len(manifest)==len(prior)+1 and manifest[:-1]==prior
assert manifest[-1]['file']=='coordination/003-module-consumers.sql'
assert manifest[-1]['sha256']==sha((wt/'next/schema'/manifest[-1]['file']).read_bytes())

clean=[];old=None;faults=None;body=0;copied=None
for item in data['results']:
    command=item['command'];lines=item.get('stdout','').splitlines()
    reports=[json.loads(line.split('=',1)[1]) for line in lines if line.startswith('CORTEX_TEST_RESULT=')]
    if '/tmp/run-old-table-guards.py' in command:
        assert item['exit_code']==1 and len(reports)==1
        old=reports[0]
    if '/tmp/next/tests/test_receipts.py' in command and item.get('exit_code')==0 and reports:
        assert not reports[-1]['failures'] and not reports[-1]['errors']
        clean.append((Path(command[-1]).name,reports[-1]['tests_run']))
    if '/tmp/next/scripts/mutate_module_consumer.py' in command:
        assert item['exit_code']==0
        rows=[json.loads(line) for line in lines if line.startswith('{')]
        faults=[row for row in rows if 'status' in row]
        summary=rows[-1]
        assert len(faults)==summary['mutants']==summary['killed']==15
        assert not summary['survivors'] and not summary['inconclusive']
        assert summary['restored_receipt']=={'tests_run':24,'failures':[],'errors':[]}
        for fault in faults:
            receipt=fault['receipt']
            assert fault['status']=='killed' and fault['exit_code']!=0
            assert not receipt['errors'] and receipt['failures']
            assert fault['expected_test'] in {f['id'].split(' (')[0] for f in receipt['failures']}
            assert all(f['phase']=='test' and f['is_assertion'] and
                       f['phase_attribution']=='traceback+active-unittest-v2' for f in receipt['failures'])
            body+=len(receipt['failures'])
    if '-c' in command and 'root.rglob' in command[-1] and item.get('exit_code')==0:
        copied=json.loads(item['stdout'])
assert old is not None and old['tests_run']==3 and not old['errors']
assert {x['id'] for x in old['failures']}=={
    'test_authorization.AuthorizationTests.test_all_business_tables_force_rls_private_functions_stay_private',
    'test_identity.IdentityTests.test_additive_kind_roles_defaults_private_memberships_and_frozen_table_count',
    'test_outbox.OutboxTests.test_additive_manifest_and_frozen_business_table_boundary'}
assert all(x['phase']=='test' and x['is_assertion'] for x in old['failures'])
assert len(clean)==27 and sum(count for _,count in clean)==344
assert ('outbox_inventory',14) in clean and ('module_consumer',24) in clean
assert len(faults)==15 and body==22
assert copied=={name.removeprefix('next/'):digest for name,digest in source.items()}
for phase,count,failures in [('c07-debug-downgrade-red-001',24,1),('c07-debug-green-005',24,0)]:
    check=json.loads((out/(phase+'.json')).read_text())
    assert check['passed'] and check['stack_removed']
    rows=[json.loads(z.split('=',1)[1]) for item in check['results']
          if item['command'][-1]=='/tmp/next/tests/module_consumer'
          for z in item.get('stdout','').splitlines() if z.startswith('CORTEX_TEST_RESULT=')]
    assert len(rows)==1 and rows[0]['tests_run']==count and len(rows[0]['failures'])==failures
    assert not rows[0]['errors']
    if failures:
        assert rows[0]['failures'][0]['id']=='test_apply_checkpoint.ApplyCheckpoint.test_complete_outcome_cannot_be_downgraded_to_poison'
        assert rows[0]['failures'][0]['phase']=='test' and rows[0]['failures'][0]['is_assertion']
print(json.dumps({'tested_commit':tested,'raw_sha256':sha(raw.read_bytes()),
                  'clean_suites':len(clean),'clean_tests':344,'old_table_guard_body_failures':3,
                  'c07_faults':15,'fault_body_assertions':body,'source_files':len(source),
                  'red_body_failures':1,'owned_removed':len(removed),'restored':True},indent=2))
