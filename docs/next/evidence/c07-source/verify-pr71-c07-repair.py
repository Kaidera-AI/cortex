"""Stdlib verification of Git-bound PR71 repair receipts; no product import."""
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

WT=Path(sys.argv[1]).resolve()
OUT=WT/'docs/next/evidence/c07-source'
sha=lambda body:hashlib.sha256(body).hexdigest()
def git(*args):return subprocess.check_output(['git','-C',str(WT),*args])
def blob(ref,path):return git('show',ref+':'+path)
def sources(ref):
    with tarfile.open(fileobj=io.BytesIO(git('archive',ref,'next'))) as archive:
        return {m.name:sha(archive.extractfile(m).read()) for m in archive.getmembers() if m.isfile()}
def report(row):
    values=[json.loads(x.split('=',1)[1]) for x in row.get('stdout','').splitlines() if x.startswith('CORTEX_TEST_RESULT=')]
    assert len(values)==1
    return values[0]
def body(value):
    assert not value['errors'] and value['failures']
    assert all(x['phase']=='test' and x['is_assertion'] and x['phase_attribution']=='traceback+active-unittest-v2' for x in value['failures'])
def cleanup(data,count):
    c=data['cleanup']
    assert c['cleanup_verified'] and c['password_discarded'] and c['lock_released']
    assert not c['pending'] and not c['owned']
    created={(x['kind'],x['id']) for x in c['events'] if x['event']=='created'}
    removed={(x['kind'],x['id']) for x in c['events'] if x['event']=='removed'}
    assert len(created)==count and created==removed
def admission(data):
    rows=[r for r in data['results'] if any(str(a).endswith('/native-memory-admission.py') for a in r['command'])]
    assert len(rows)==1 and rows[0]['exit_code']==0
    value=json.loads(rows[0]['stdout'])
    assert value['passed'] and value['floor_percent']==35 and value['resource_operations']==0
    assert value['mac_free_percent']>=35 and value['vm_free_percent']>=35
    mem=value['vm_meminfo_kB']
    assert abs(value['vm_free_percent']-100*mem['MemAvailable']/mem['MemTotal'])<1e-9
    gate_index=data['results'].index(rows[0])
    creates=[i for i,r in enumerate(data['results']) if r['command'][:3]==['podman','pod','create'] or r['command'][:3]==['podman','run','-d']]
    assert creates and gate_index<min(creates)
    return value

assert (OUT/'pr71-repair-binding.json').read_bytes()==blob('HEAD','docs/next/evidence/c07-source/pr71-repair-binding.json')
binding=json.loads((OUT/'pr71-repair-binding.json').read_text())
for path,digest in binding['published_inputs'].items():
    assert sha((WT/path).read_bytes())==sha(blob('HEAD',path))==digest,path
red=[json.loads(x) for x in (OUT/'reviewer-source-red.log').read_text().splitlines() if x.startswith('{')]
assert red[-1]==dict(tests_run=3,failures=5,errors=0)
for path,digest in red[0]['source_sha256'].items():assert sha(blob(red[0]['head'],path))==digest
source_green=json.loads((OUT/'reviewer-source-green.log').read_text().splitlines()[-1])
assert (source_green['tests_run'],source_green['failures'],source_green['errors'])==(3,0,0)
assert source_green['control_sha256']==sha(blob('HEAD','docs/next/evidence/c07-source/frozen/mike-repair-manifest.py'))
for path,digest in source_green['source_sha256'].items():assert sha(blob(source_green['head'],path))==sha(blob('HEAD',path))==digest
full=json.loads((OUT/binding['full']).read_text())
assert full['passed'] and full['stack_removed'] and not full['mutation_errors'] and full['final_inventory_error'] is None
assert full['source_sha256']==sources(full['tree'])==sources('HEAD')
assert git('merge-base','--is-ancestor',binding['main'],full['tree'])==b''
assert full['limits']==dict(cpus=2,memory='1g',pg_memory='768m',driver_memory='256m')
assert full['native_arch']=='arm64' and full['network']=='none; private loopback'
assert not full['published_ports'] and not full['bind_mounts']
cleanup(full,3);mem=admission(full)
assert full['controller_sha256']==sha(blob(full['tree'],'docs/next/evidence/c07-source/run-review-pg.py'))==sha(blob('HEAD','docs/next/evidence/c07-source/run-review-pg.py'))
for name,digest in full['tool_input_sha256'].items():
    if name in ('actual_wheels','copied_wheels'):continue
    path=('docs/next/evidence/c06-source/replay_lifecycle.py' if name=='shared/replay_lifecycle.py' else 'docs/next/evidence/c07-source/'+name)
    assert sha(blob(full['tree'],path))==sha(blob('HEAD',path))==digest,path
wheels=json.loads((WT/'docs/next/evidence/c06-source/offline-wheel-inputs.json').read_text())['wheels']
assert full['tool_input_sha256']['actual_wheels']==full['tool_input_sha256']['copied_wheels']==wheels
clean=[];old=[];faults=[];copied=None
recipes=ast.parse(blob('HEAD','next/scripts/mutate_module_consumer.py'))
expected=[]
# Resolve only literal recipe rows and symbolic paths, never execute the mutator.
names={n.targets[0].id:ast.literal_eval(n.value) for n in recipes.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and isinstance(n.value,ast.Constant)}
def rows(node):
    return [tuple(names[x.id] if isinstance(x,ast.Name) else ast.literal_eval(x) for x in row.elts) for row in node.elts]
for n in recipes.body:
    if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='RECIPES' for t in n.targets):expected+=rows(n.value)
    if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute) and isinstance(n.value.func.value,ast.Name) and n.value.func.value.id=='RECIPES' and n.value.func.attr=='extend':expected+=rows(n.value.args[0])
assert len(expected)==24 and len({r[0] for r in expected})==24
for r in full['results']:
    cmd=r['command']
    if '/tmp/next/tests/test_receipts.py' in cmd:
        value=report(r);assert r['exit_code']==0 and not value['failures'] and not value['errors'];clean.append((Path(cmd[-1]).name,value['tests_run']))
    if '/tmp/run-old-table-guards.py' in cmd:
        value=report(r);assert r['exit_code']==1 and value['tests_run']==3;body(value);old=value['failures']
    if '/tmp/next/scripts/mutate_module_consumer.py' in cmd:
        assert r['exit_code']==0
        values=[json.loads(x) for x in r['stdout'].splitlines() if x.startswith('{')]
        faults=[x for x in values if 'status' in x];summary=values[-1]
        assert summary['mutants']==summary['killed']==len(faults)==24 and not summary['survivors'] and not summary['inconclusive']
        assert values[0]['baseline']==summary['restored_receipt']==dict(tests_run=35,failures=[],errors=[])
        assert values[0]['source_sha256']==summary['restored_source_sha256']
        for fault,recipe in zip(faults,expected):
            label,path,before,after,test=recipe
            assert (fault['mutation'],fault['source'],fault['expected_test'])==(label,path,test)
            assert fault['status']=='killed' and fault['exit_code']==1 and fault['receipt']['tests_run']==1
            body(fault['receipt']);assert test in {x['id'].split(' (')[0] for x in fault['receipt']['failures']}
            assert fault['original_source_sha256']=={name:sha(blob('HEAD','next/'+name)) for name in {x[1] for x in expected}|{'schema/manifest.json'}}
            changed=blob('HEAD','next/'+path).decode();assert changed.count(before)==1
            effective=changed.replace(before,after).encode();effective_hashes=dict(fault['original_source_sha256']);effective_hashes[path]=sha(effective)
            if path=='schema/coordination/003-module-consumers.sql':
                manifest=json.loads(blob('HEAD','next/schema/manifest.json'));manifest['migrations'][-1]['sha256']=sha(effective)
                effective_hashes['schema/manifest.json']=sha((json.dumps(manifest,indent=2)+'\n').encode())
            assert effective_hashes==fault['effective_source_sha256']
    if '-c' in cmd and 'root.rglob' in cmd[-1] and r['exit_code']==0:copied=json.loads(r['stdout'])
assert len(clean)==27 and sum(n for _,n in clean)==379 and ('module_consumer',35) in clean
assert {x['id'] for x in old}=={'test_authorization.AuthorizationTests.test_all_business_tables_force_rls_private_functions_stay_private','test_identity.IdentityTests.test_additive_kind_roles_defaults_private_memberships_and_frozen_table_count','test_outbox.OutboxTests.test_additive_manifest_and_frozen_business_table_boundary'}
assert len(faults)==24
assert copied=={p.removeprefix('next/'):d for p,d in full['source_sha256'].items()}
for filename,count,failures in [('c07-review-red-002.json',34,55),('c07-review-green-001.json',34,0),('c07-review-red-scan-001.json',35,1)]:
    data=json.loads((OUT/filename).read_text());assert data['passed'] and data['stack_removed'];cleanup(data,3);admission(data)
    assert data['source_sha256']==sources(data['tree'])
    selected=[r for r in data['results'] if '/tmp/next/tests/module_consumer' in r['command']];assert len(selected)==1
    value=report(selected[0]);assert value['tests_run']==count and len(value['failures'])==failures and not value['errors']
    if failures:body(value)
native=json.loads((WT/binding['backup_native']).read_text())
assert native['passed'] and not native['errors'] and native['local_tests']==7
cleanup(native,1);backup_mem=admission(native)
local=[r for r in native['results'] if any(str(a).endswith('/tests/backup_producer') for a in r['command'])];assert len(local)==1 and local[0]['exit_code']==0 and report(local[0])==dict(tests_run=7,failures=[],errors=[])
assert native['limits']==dict(cpus=2,memory='1g') and not native['published_ports'] and not native['bind_mounts'] and native['synthetic_only']
for path,digest in native['source_sha256'].items():assert sha(blob(native['tree'],path))==sha(blob('HEAD',path))==digest
assert all(native['native'][k] for k in ('pg_verifybackup_passed','synthetic_age_roundtrip','base_only_refused','gap_refused','unbound_interval_refused'))
assert len(native['native']['wal_segments'])==4 and native['native']['base_files']>0
for path in ('next/src/cortex_core/backup.py','next/tests/backup_producer/test_manifest.py','next/scripts/mutate_backup.py'):
    assert blob('HEAD',path)==blob(binding['before'],path),path
backup=json.loads((WT/'docs/next/evidence/o01a-source/backup-forward-mutants-002.jsonl').read_text().splitlines()[-1])
assert backup['mutants']==backup['killed']==7 and not backup['inconclusive'] and backup['source_sha256']==sha(blob('HEAD','next/src/cortex_core/backup.py'))
assert backup['baseline']['receipt']==backup['restored']['receipt']==dict(tests_run=7,failures=[],errors=[])
for fault in backup['rows']:
    assert fault['status']=='killed' and fault['exit_code']==1;body(fault['receipt'])
    assert fault['expected_test'] in {x['id'].split(' (')[0] for x in fault['receipt']['failures']}
manifest=json.loads(blob('HEAD','next/schema/manifest.json'))['migrations'];prior=json.loads(blob(binding['main'],'next/schema/manifest.json'))['migrations']
assert len(manifest)==len(prior)+1 and manifest[:-1]==prior and manifest[-1]['file']=='coordination/003-module-consumers.sql'
assert manifest[-1]['sha256']==sha(blob('HEAD','next/schema/'+manifest[-1]['file']))
print(json.dumps(dict(passed=True,publication_head=git('rev-parse','HEAD').decode().strip(),tested_commit=full['tree'],native_backup_commit=native['tree'],source_files=len(full['source_sha256']),clean_suites=27,clean_tests=379,module_tests=35,module_faults=24,body_assertions=sum(len(x['receipt']['failures']) for x in faults),backup_tests=7,backup_faults=7,base_files=native['native']['base_files'],wal_segments=4,cleanup_verified=True,mac_free_percent=mem['mac_free_percent'],vm_free_percent=mem['vm_free_percent'],backup_mac_free_percent=backup_mem['mac_free_percent'],backup_vm_free_percent=backup_mem['vm_free_percent']),indent=2))
