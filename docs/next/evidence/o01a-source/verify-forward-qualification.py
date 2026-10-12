"""Stdlib Git/receipt verification of the integrated PR71 forward qualification."""
import ast
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

WT = Path(sys.argv[1]).resolve()
OUT = WT / 'docs/next/evidence/o01a-source'
sha = lambda data: hashlib.sha256(data).hexdigest()
def git(*args):
    return subprocess.check_output(['git', '-C', str(WT), *args])
def committed(ref, path):
    return git('show', ref + ':' + path)
def sources(ref):
    archive = tarfile.open(fileobj=io.BytesIO(git('archive', ref, 'next')))
    return {m.name: sha(archive.extractfile(m).read()) for m in archive.getmembers() if m.isfile()}

binding = json.loads((OUT / 'forward-qualification-binding.json').read_text())
native = json.loads((OUT / binding['native_file']).read_text())
assert native['passed'] and not native['errors'] and native['local_tests'] == 7
assert native['cleanup']['cleanup_verified'] and native['cleanup']['password_discarded'] and native['cleanup']['lock_released']
assert not native['cleanup']['pending'] and not native['cleanup']['owned']
created = {(x['kind'],x['id']) for x in native['cleanup']['events'] if x['event']=='created'}
removed = {(x['kind'],x['id']) for x in native['cleanup']['events'] if x['event']=='removed'}
assert len(created)==1 and created==removed
for path,digest in native['source_sha256'].items():
    assert sha(committed(native['tree'],path)) == sha(committed('HEAD',path)) == digest
assert native['native']['pg_verifybackup_passed'] and native['native']['synthetic_age_roundtrip']
assert all(native['native'][key] for key in ('base_only_refused','gap_refused','unbound_interval_refused'))
assert len(native['native']['wal_segments']) == 4 and native['native']['base_files'] > 0

faults = json.loads((OUT / binding['backup_fault_file']).read_text().splitlines()[-1])
assert faults['mutants'] == faults['killed'] == 7 and not faults['inconclusive']
assert faults['source_sha256'] == sha(committed('HEAD','next/src/cortex_core/backup.py'))
assert faults['baseline']['receipt'] == faults['restored']['receipt'] == {'tests_run':7,'failures':[],'errors':[]}
for row in faults['rows']:
    value=row['receipt']; assert row['status']=='killed' and row['exit_code']==1 and not value['errors']
    assert row['expected_test'] in {f['id'].split(' (')[0] for f in value['failures']}
    assert all(f['phase']=='test' and f['is_assertion'] and f['phase_attribution']=='traceback+active-unittest-v2' for f in value['failures'])
    assert value['tests_run'] == (1 if row['name']=='encrypted_output' else 7)

pg=json.loads((OUT / binding['inherited_file']).read_text())
module=json.loads((OUT / binding['module_fault_file']).read_text())
for data in (pg,module):
    assert data['passed'] and data['stack_removed'] and not data['mutation_errors'] and data['final_inventory_error'] is None
    assert data['source_sha256'] == sources(data['tree']) == sources('HEAD')
    assert data['cleanup']['cleanup_verified'] and data['cleanup']['password_discarded'] and data['cleanup']['lock_released']
    assert not data['cleanup']['owned'] and not data['cleanup']['pending']
    created={(r['kind'],r['id']) for r in data['cleanup']['events'] if r['event']=='created'}
    removed={(r['kind'],r['id']) for r in data['cleanup']['events'] if r['event']=='removed'}
    assert len(created)==3 and created==removed
    assert data['limits']=={'cpus':2,'memory':'1g','pg_memory':'768m','driver_memory':'256m'}
    assert not data['published_ports'] and not data['bind_mounts']
    assert data['controller_sha256']==sha(committed(data['tree'],'docs/next/evidence/c07-source/run-core-pg.py'))
    assert data['controller_sha256']==sha(committed('HEAD','docs/next/evidence/c07-source/run-core-pg.py'))
clean=[]
for row in pg['results']:
    if '/tmp/next/tests/test_receipts.py' in row['command']:
        value=json.loads(next(line.split('=',1)[1] for line in row['stdout'].splitlines() if line.startswith('CORTEX_TEST_RESULT=')))
        assert row['exit_code']==0 and not value['errors'] and not value['failures'];clean.append(value['tests_run'])
assert len(clean)==27 and sum(clean)==368
recipe_tree=ast.parse(committed('HEAD','next/scripts/mutate_module_consumer.py'))
expected=len(next(n.value.elts for n in recipe_tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='RECIPES' for t in n.targets)))
module_faults=[]
for row in module['results']:
    if '/tmp/next/scripts/mutate_module_consumer.py' in row['command']:
        assert row['exit_code']==0
        records=[json.loads(line) for line in row['stdout'].splitlines() if line.startswith('{')]
        module_faults=[v for v in records if 'status' in v];summary=records[-1]
        assert summary['mutants']==summary['killed']==len(module_faults)==expected
        assert not summary['survivors'] and not summary['inconclusive']
        for fault in module_faults:
            value=fault['receipt'];assert fault['status']=='killed' and not value['errors']
            assert fault['expected_test'] in {f['id'].split(' (')[0] for f in value['failures']}
            assert all(f['phase']=='test' and f['is_assertion'] and f['phase_attribution']=='traceback+active-unittest-v2' for f in value['failures'])
assert len(module_faults)==expected
for path,digest in binding['published_inputs'].items():
    assert sha((WT/path).read_bytes())==sha(committed('HEAD',path))==digest
print(json.dumps({'passed':True,'publication_head':git('rev-parse','HEAD').decode().strip(),
                  'native_tested_commit':native['tree'],'inherited_tested_commit':pg['tree'],
                  'source_files':len(pg['source_sha256']),'inherited_pg_tests':368,
                  'backup_tests':7,'backup_faults':7,'module_faults':expected,
                  'native_base_files':native['native']['base_files'],'wal_segments':4,
                  'all_cleanup_verified':True,'published_inputs':len(binding['published_inputs'])},indent=2))
