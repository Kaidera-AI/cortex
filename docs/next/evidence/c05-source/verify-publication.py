"""Check publication bytes and canonical custody pointers using stdlib only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

index = Path(sys.argv[1])
wt = Path(sys.argv[2])
data = json.loads(index.read_text())
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
                  'members': len(data['members']), 'all_digests_sizes_canonical_pointers_match': True}))
