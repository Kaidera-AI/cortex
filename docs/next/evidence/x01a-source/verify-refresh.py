"""Stdlib independent Git/source/body/native-stack custody for X01a refresh."""
import ast,hashlib,io,json,subprocess,sys,tarfile
from pathlib import Path
WT=Path(sys.argv[1]).resolve();OUT=WT/'docs/next/evidence/x01a-source'
sha=lambda b:hashlib.sha256(b).hexdigest()
def git(*args):return subprocess.check_output(['git','-C',str(WT),*args])
def blob(ref,path):return git('show',ref+':'+path)
def sources(ref):
    with tarfile.open(fileobj=io.BytesIO(git('archive',ref,'next'))) as a:
        return {m.name:sha(a.extractfile(m).read()) for m in a.getmembers() if m.isfile()}
def report(row):
    r=[json.loads(l.split('=',1)[1]) for l in row['stdout'].splitlines() if l.startswith('CORTEX_TEST_RESULT=')]
    assert len(r)==1;return r[0]
path='docs/next/evidence/x01a-source/refresh-binding.json'
assert (WT/path).read_bytes()==blob('HEAD',path);b=json.loads((WT/path).read_text())
assert b['base']=='87fd43d6cb6d5d91361f88d71698f2b3e3721b65'
assert subprocess.run(['git','-C',str(WT),'merge-base','--is-ancestor',b['base'],'HEAD']).returncode==0
for path,digest in b['published_inputs'].items():assert sha((WT/path).read_bytes())==sha(blob('HEAD',path))==digest,path
pre=json.loads((OUT/'pre-change-custody.json').read_text())
for path,digest in pre['source_sha256'].items():assert sha(blob(pre['head'],path))==sha(blob('HEAD',path))==sha((WT/path).read_bytes())==digest,path
for name,digest in pre['lineage_sha256'].items():assert sha((OUT/'lineage'/name).read_bytes())==digest
faults=[]
for name in ['pre-integration-faults.jsonl','current-faults.jsonl']:
    d=json.loads((OUT/name).read_text().splitlines()[-1]);assert d['mutants']==d['killed']==15 and not d['inconclusive']
    assert d['source_sha256']==pre['source_sha256']['next/src/cortex_core/recovery_fixture.py']
    for r in d['rows']:
        v=r['receipt'];assert r['status']=='killed' and r['exit_code']==1 and v['tests_run']==15 and not v['errors']
        assert r['expected_test'] in {f['id'].split(' (')[0] for f in v['failures']}
        assert all(f['phase']=='test' and f['is_assertion'] and f['phase_attribution']=='traceback+active-unittest-v2' for f in v['failures'])
    if name=='current-faults.jsonl':faults=d['rows']
assert sum(len(r['receipt']['failures']) for r in faults)==18
n=json.loads((OUT/b['native']).read_text());assert n['passed'] and n['stack_removed'] and not n['mutation_errors'] and n['final_inventory_error'] is None
assert n['source_sha256']==sources(n['tree'])==sources('HEAD');assert len(n['source_sha256'])==490
for path,digest in n['source_sha256'].items():assert sha((WT/path).read_bytes())==digest,path
assert n['limits']==dict(cpus=2,memory='1g',pg_memory='768m',driver_memory='256m')
assert n['native_arch']=='arm64' and n['network']=='none; private loopback' and not n['published_ports'] and not n['bind_mounts']
assert n['controller_sha256']==sha(blob(n['tree'],'docs/next/evidence/x01a-source/run-inherited-pg.py'))==sha(blob('HEAD','docs/next/evidence/x01a-source/run-inherited-pg.py'))
for name,digest in n['tool_input_sha256'].items():
    if isinstance(digest,dict):continue
    path=('docs/next/evidence/x01a-source/'+name[5:] if name.startswith('x01a/') else 'docs/next/evidence/c06-source/'+name[7:] if name.startswith('shared/') else 'docs/next/evidence/c07-source/'+name)
    assert sha(blob(n['tree'],path))==sha(blob('HEAD',path))==digest,path
w=json.loads((WT/'docs/next/evidence/c06-source/offline-wheel-inputs.json').read_text())['wheels'];assert n['tool_input_sha256']['actual_wheels']==n['tool_input_sha256']['copied_wheels']==w
clean={}
for r in n['results']:
    if '/tmp/next/tests/test_receipts.py' in r['command']:
        v=report(r);assert r['exit_code']==0 and not v['failures'] and not v['errors'];clean[Path(r['command'][-1]).name]=v['tests_run']
assert clean==dict(module_consumer=44,backup_producer=7,backup_validation=16,recovery_fixture=15)
old=[r for r in n['results'] if '/tmp/run-old-table-guards.py' in r['command']];assert len(old)==1
v=report(old[0]);assert old[0]['exit_code']==1 and v['tests_run']==3 and len(v['failures'])==3 and not v['errors']
assert all(f['phase']=='test' and f['is_assertion'] for f in v['failures'])
a=[(i,r) for i,r in enumerate(n['results']) if any(str(x).endswith('/native-memory-admission.py') for x in r['command'])];assert len(a)==1
i,r=a[0];mem=json.loads(r['stdout']);assert r['exit_code']==0 and mem['passed'] and mem['floor_percent']==35 and mem['resource_operations']==0
assert mem['mac_free_percent']>=35 and mem['vm_free_percent']>=35
v=mem['vm_meminfo_kB'];assert abs(mem['vm_free_percent']-100*v['MemAvailable']/v['MemTotal'])<1e-9
creates=[j for j,r in enumerate(n['results']) if r['command'][:3]==['podman','pod','create'] or r['command'][:3]==['podman','run','-d']];assert creates and i<min(creates)
c=n['cleanup'];assert c['cleanup_verified'] and c['password_discarded'] and c['lock_released'] and not c['pending'] and not c['owned']
created={(r['kind'],r['id']) for r in c['events'] if r['event']=='created'};removed={(r['kind'],r['id']) for r in c['events'] if r['event']=='removed'};assert len(created)==3 and created==removed
age=json.loads((OUT/'age-linux-input.json').read_text());assert age['tag']=='v1.3.1' and age['elf_machine']==183 and not age['host_install'] and not age['host_execution'] and not age['release_trust_admission']
versions=[r for r in n['results'] if r['command'][-2:]==['/tmp/age-bin/age','--version']];assert len(versions)==1 and versions[0]['exit_code']==0 and versions[0]['stdout'].strip()=='v1.3.1'
age_hashes=[r for r in n['results'] if r['command'][:3]==['podman','exec','kaidera-test-core-driver-1'] and any('/tmp/age-bin' in str(x) and 'hashlib' in str(x) for x in r['command'])];assert len(age_hashes)==1 and json.loads(age_hashes[0]['stdout'])==age['binary_sha256']

# Inert fixture starts no process; unit/native-stack execution is author verification, not X01b.
tree=ast.parse(blob('HEAD','next/src/cortex_core/recovery_fixture.py'));assert not any(isinstance(x,(ast.Import,ast.ImportFrom)) and ('subprocess' in ast.unparse(x) or 'socket' in ast.unparse(x)) for x in ast.walk(tree))
print(json.dumps(dict(passed=True,publication_head=git('rev-parse','HEAD').decode().strip(),tested_commit=n['tree'],source_files=490,controls=clean,tests=sum(clean.values()),faults=15,body_assertions=18,restored=True,owned_removed=3,mac_free_percent=mem['mac_free_percent'],vm_free_percent=mem['vm_free_percent'],host_commands_by_fixture=0,x01b_qualification=False),indent=2))
