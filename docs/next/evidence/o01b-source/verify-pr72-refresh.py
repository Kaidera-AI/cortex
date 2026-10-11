"""Stdlib exact-source verification of refreshed PR72 native and fault receipts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

WT = Path(sys.argv[1]).resolve()
OUT = WT/'docs/next/evidence/o01b-source'
sha = lambda body: hashlib.sha256(body).hexdigest()
def git(*args): return subprocess.check_output(['git','-C',str(WT),*args])
def blob(ref,path): return git('show',ref+':'+path)
def report(row):
    values=[json.loads(line.split('=',1)[1]) for line in row['stdout'].splitlines()
            if line.startswith('CORTEX_TEST_RESULT=')]
    assert len(values)==1
    return values[0]
def cleanup(data,count):
    c=data['cleanup']
    assert c['cleanup_verified'] and c['password_discarded'] and c['lock_released']
    assert not c['pending'] and not c['owned']
    created={(r['kind'],r['id']) for r in c['events'] if r['event']=='created'}
    removed={(r['kind'],r['id']) for r in c['events'] if r['event']=='removed'}
    assert len(created)==count and created==removed

def admission(data):
    matches=[(i,r) for i,r in enumerate(data['results'])
             if any(str(a).endswith('/native-memory-admission.py') for a in r['command'])]
    assert len(matches)==1
    i,row=matches[0];v=json.loads(row['stdout'])
    assert row['exit_code']==0 and v['passed'] and v['floor_percent']==35 and v['resource_operations']==0
    assert v['mac_free_percent']>=35 and v['vm_free_percent']>=35
    mem=v['vm_meminfo_kB']
    assert abs(v['vm_free_percent']-100*mem['MemAvailable']/mem['MemTotal'])<1e-9
    creates=[n for n,r in enumerate(data['results']) if r['command'][:3] in
             (['podman','pod','create'],['podman','run','-d'])]
    assert creates and i<min(creates)
    return v

binding_path='docs/next/evidence/o01b-source/refresh-binding.json'
assert (WT/binding_path).read_bytes()==blob('HEAD',binding_path)
b=json.loads((WT/binding_path).read_text())
assert b['base']=='435da3df7a253b3e0f5a41dd0d188320a40f882b'
assert subprocess.run(['git','-C',str(WT),'merge-base','--is-ancestor',b['base'],'HEAD']).returncode==0
for path,digest in b['published_inputs'].items():
    assert sha((WT/path).read_bytes())==sha(blob('HEAD',path))==digest,path
protected=['next/src/cortex_core/backup.py','next/src/cortex_core/backup_validation.py',
           'next/tests/backup_validation/test_validation.py','next/tests/backup_producer/test_manifest.py']
for path in protected: assert blob('HEAD',path)==blob(b['accepted'],path),path
for path in ['next/src/cortex_core/modules/consumer.py','next/src/cortex_core/modules/protocol.py',
             'next/schema/coordination/003-module-consumers.sql','next/schema/manifest.json',
             'next/tests/module_consumer/test_manifest.py']:
    assert blob('HEAD',path)==blob(b['base'],path),path
next_paths=git('ls-tree','-r','--name-only','HEAD','next').decode().splitlines()
current={p:sha(blob('HEAD',p)) for p in next_paths}
assert len(current)==486
for path,digest in current.items(): assert sha((WT/path).read_bytes())==digest,path

red=json.loads((OUT/'refresh/o01b-driver-red.jsonl').read_text().splitlines()[-1])
assert red['mutants']==8 and red['killed']==0 and len(red['inconclusive'])==8
assert all(r['receipt']['tests_run']==16 and not r['receipt']['errors'] for r in red['rows'])
body_count=0
for name,count,tests in [('o01b-faults-green-002.jsonl',9,16),('o01a-faults-green-002.jsonl',7,7)]:
    data=json.loads((OUT/'refresh'/name).read_text().splitlines()[-1])
    assert data['mutants']==data['killed']==count and not data['inconclusive']
    if isinstance(data['source_sha256'],dict):
        for path,digest in data['source_sha256'].items(): assert current['next/src/cortex_core/'+path]==digest
    else: assert current['next/src/cortex_core/backup.py']==data['source_sha256']
    assert data['baseline']['receipt']==data['restored']['receipt']==dict(tests_run=tests,failures=[],errors=[])
    for row in data['rows']:
        r=row['receipt'];failures=r['failures']
        assert row['status']=='killed' and row['exit_code']==1 and not r['errors']
        expected_count=1 if row.get('test_selection')=='declared expected test only' else tests
        assert r['tests_run']==expected_count and failures
        assert row['expected_test'] in {f['id'] for f in failures}
        assert all(f['phase']=='test' and f['is_assertion'] and
                   f['phase_attribution']=='traceback+active-unittest-v2' for f in failures)
        body_count+=len(failures)
assert body_count==24

core=json.loads((WT/b['core_native']).read_text())
backup=json.loads((WT/b['backup_native']).read_text())
for data,n in [(core,3),(backup,1)]:
    assert data['passed'] and not data.get('errors',[]) and not data.get('mutation_errors',[])
    for path,digest in data['source_sha256'].items():
        assert sha(blob(data['tree'],path))==sha(blob('HEAD',path))==sha((WT/path).read_bytes())==digest,path
    assert {p:v for p,v in data['source_sha256'].items() if p.startswith('next/')}==current
    cleanup(data,n)
    assert not data['published_ports'] and not data['bind_mounts']
    assert data['limits']['cpus']==2 and data['limits']['memory']=='1g'
core_admission=admission(core);backup_admission=admission(backup)
assert backup['native']['admission']==backup_admission
for name,digest in core['tool_input_sha256'].items():
    if isinstance(digest,dict): continue
    path=('docs/next/evidence/c06-source/'+name[7:] if name.startswith('shared/')
          else 'docs/next/evidence/c07-source/'+name)
    assert sha(blob('HEAD',path))==sha(blob(core['tree'],path))==digest,path
rows=[r for r in core['results'] if any(str(a).endswith('/module_consumer') for a in r['command'])]
assert len(rows)==1 and rows[0]['exit_code']==0
assert report(rows[0])==dict(tests_run=44,failures=[],errors=[])
for method in ['test_ninth_migration_and_exact_three_tables_are_additive','test_new_tables_force_rls_and_request_has_no_direct_write']:
    assert method in rows[0]['stderr'],method
assert core['stack_removed'] and core['network']=='none; private loopback' and core['native_arch']=='arm64'
for suite,count in [('backup_producer',7),('backup_validation',16)]:
    r=next(r for r in backup['results'] if r['command']==['test_receipts',suite])
    assert r['exit_code']==0 and report(r)==dict(tests_run=count,failures=[],errors=[])
v=backup['native']
assert v['pg_version']=='18.4' and v['native_arch']=='arm64' and v['segment_bytes']==16777216
assert v['before']==[1,1,1,0] and v['after_source']==v['after_recovery']==[2,2,2,1]
assert v['mutation_overlapped_backup'] and v['replay_lsn']==v['archive_end_lsn']!=v['requested_end_lsn']
assert len(v['wal_segments'])==4 and v['base_files']==1112 and v['blob_members']==2 and v['verified_member_count']==1119
for key in ['mixed_snapshot_refused','missing_blob_refused','altered_vector_refused','altered_schema_refused',
            'altered_wal_refused','postcheck_base_refused','postcheck_wal_refused','postcheck_no_ciphertext',
            'missing_identity_refused','bad_manifest_digest_refused']:
    assert v[key],key
assert backup['synthetic_only'] and all('18.' in v['client_tools'][k] for k in ['pg_verifybackup','pg_waldump'])
print(json.dumps(dict(passed=True,publication_head=git('rev-parse','HEAD').decode().strip(),
                     core_tested_commit=core['tree'],backup_tested_commit=backup['tree'],source_files=len(current),
                     module_tests=44,backup_tests=23,faults=16,body_assertions=body_count,
                     base_files=v['base_files'],wal_segments=4,encrypted_members=v['verified_member_count'],
                     owned_removed=4,core_mac_free_percent=core_admission['mac_free_percent'],
                     core_vm_free_percent=core_admission['vm_free_percent'],
                     backup_mac_free_percent=backup_admission['mac_free_percent'],
                     backup_vm_free_percent=backup_admission['vm_free_percent']),indent=2))
