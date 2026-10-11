"""Stdlib-only validation of receipt, frozen bytes and accepted-plan custody."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

receipt_path = Path(sys.argv[1])
wt = Path(sys.argv[2])
out = Path(__file__).resolve().parent
receipt = json.loads(receipt_path.read_bytes())
assert receipt['passed'] and receipt['stack_removed']
cleanup = receipt['cleanup']
assert cleanup['cleanup_verified'] and not cleanup['pending'] and not cleanup['owned']
assert cleanup['password_discarded'] and cleanup['lock_released']
observed = {str(p.relative_to(wt)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (wt/'next').rglob('*') if p.is_file()}
assert observed == receipt['source_sha256'], 'all NEXT bytes must match the tested receipt'
archive = subprocess.check_output(['git','-C',str(wt),'archive',receipt['tree'],'next'])
with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
    committed = {member.name:hashlib.sha256(tree.extractfile(member).read()).hexdigest()
                 for member in tree.getmembers() if member.isfile()}
assert committed == observed, 'all NEXT bytes must match the committed tested tree'
pre = json.loads((out/'pre-edit.json').read_bytes())
protected = pre['protected_predecessor_inputs']
for relative,digest in protected.items():
    if relative != 'next/schema/manifest.json':
        assert observed[relative] == digest, ('protected_predecessor',relative)
previous = json.loads(subprocess.check_output(['git','-C',str(wt),'show',pre['base']+':next/schema/manifest.json']))
current = json.loads((wt/'next/schema/manifest.json').read_bytes())
assert current['migrations'][:-1] == previous['migrations']
assert current['migrations'][-1]['id'] == 'auth-0003'
frozen = json.loads((out/'frozen-inputs.json').read_bytes())
for fixture in frozen['fixtures']:
    relative = fixture['path']
    assert observed[relative] == fixture['sha256']
    first = subprocess.check_output(['git','-C',str(wt),'show',fixture['commit']+':'+relative])
    assert hashlib.sha256(first).hexdigest() == fixture['sha256'], ('frozen_commit',relative)
assert hashlib.sha256(Path(pre['accepted_plan']).read_bytes()).hexdigest() == pre['accepted_plan_sha256']
assert hashlib.sha256((out/'run-core-pg.py').read_bytes()).hexdigest() == receipt['controller_sha256']
print(json.dumps({'receipt_sha256':hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                  'tested_sha':receipt['tree'],'all_next_git_bytes':len(observed),
                  'protected_predecessors':len(protected)-1,'frozen_suites':len(frozen['fixtures']),
                  'prior_manifest_entries':len(previous['migrations']),
                  'cleanup_verified':True,'accepted_plan_and_controller_match':True}))
