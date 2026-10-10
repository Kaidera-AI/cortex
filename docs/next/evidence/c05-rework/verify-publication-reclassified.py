"""Check C05's complete source/evidence publication against an external Git inventory."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
base = Path('docs/next/evidence/c05-rework')
expected_path = wt / base / 'publication-expected-reclassified.json'
index_path = wt / base / 'publication-index-reclassified-001.json'
expected_bytes = expected_path.read_bytes()
expected = json.loads(expected_bytes)
index = json.loads(index_path.read_bytes())
sha = lambda body: hashlib.sha256(body).hexdigest()
assert index['expected_sha256'] == sha(expected_bytes)
assert index['members'] == expected['members']
assert expected['tested_commit'] == '16677ed2f1c72ff18f5a838ebdf0ca1fd67f1fba'
assert sha(subprocess.check_output(['git', '-C', str(wt), 'show', 'HEAD:' + str(base / expected_path.name)])) == sha(expected_bytes)
excluded = {expected_path.relative_to(wt), index_path.relative_to(wt)}
actual = {str(p.relative_to(wt)) for dirname in ('next', 'docs/next/evidence/c05-rework', 'docs/next/evidence/c05-source')
          for p in (wt / dirname).rglob('*') if p.is_file() and p.relative_to(wt) not in excluded}
assert set(expected['members']) == actual, ('membership', sorted(actual - set(expected['members'])), sorted(set(expected['members']) - actual))
tracked = set(subprocess.check_output(['git', '-C', str(wt), 'ls-files'], text=True).splitlines())
assert actual <= tracked, ('untracked', sorted(actual - tracked))
for relative, meta in expected['members'].items():
    current = (wt / relative).read_bytes()
    assert len(current) == meta['bytes'] and sha(current) == meta['sha256'], ('published_digest', relative)
    if relative.startswith('next/'):
        bound = subprocess.check_output(['git', '-C', str(wt), 'show', expected['tested_commit'] + ':' + relative])
    else:
        bound = (wt / meta['canonical_source']).read_bytes()
    assert sha(bound) == meta['sha256'], ('canonical_digest', relative)
assert expected['source_files'] == len([p for p in actual if p.startswith('next/')]) == 406
print(json.dumps({'index_sha256':sha(index_path.read_bytes()),'expected_sha256':sha(expected_bytes),
                  'members':len(actual),'source_files':406,'complete_membership':True,'canonical_bytes':True}))
