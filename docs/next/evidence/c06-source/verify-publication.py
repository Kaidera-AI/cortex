"""Check publication bytes and canonical custody pointers using stdlib only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

index = Path(sys.argv[1])
wt = Path(sys.argv[2])
provenance = Path(sys.argv[3]) if len(sys.argv) > 3 else wt
data = json.loads(index.read_text())
expected_path = Path(__file__).with_name('publication-expected.json')
expected_bytes = expected_path.read_bytes()
expected_rel = 'docs/next/evidence/c06-source/publication-expected.json'
committed_expected = subprocess.check_output(['git', '-C', str(provenance), 'show', 'HEAD:' + expected_rel])
assert expected_bytes == committed_expected, 'expected_inventory_not_git_bound'
expected = json.loads(expected_bytes)
source = set(expected['source_paths'])
evidence = set(expected['evidence_paths'])
assert source.isdisjoint(evidence) and source | evidence == set(data['members']), 'expected_inventory_mismatch'
git_source = set(subprocess.check_output(['git', '-C', str(provenance), 'ls-tree', '-r', '--name-only',
                                          expected['tested_source_commit'], 'next'], text=True).splitlines())
assert source == git_source, 'expected_source_tree_mismatch'
for relative, member in data['members'].items():
    published = (wt / relative).read_bytes()
    assert hashlib.sha256(published).hexdigest() == member['sha256'], ('published_digest', relative)
    assert len(published) == member['bytes'], ('published_size', relative)
    canonical = member['canonical_source']
    if canonical.startswith('product Git at tested '):
        sha = canonical.removeprefix('product Git at tested ')
        original = subprocess.check_output(['git', '-C', str(wt), 'show', sha + ':' + relative])
    else:
        assert Path(canonical).is_file(), ('missing_canonical_source', relative, canonical)
        original = Path(canonical).read_bytes()
    assert hashlib.sha256(original).hexdigest() == member['sha256'], ('canonical_digest', relative)
print(json.dumps({'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                  'expected_inventory_sha256': hashlib.sha256(expected_bytes).hexdigest(),
                  'members': len(data['members']), 'all_digests_sizes_canonical_pointers_match': True}))
