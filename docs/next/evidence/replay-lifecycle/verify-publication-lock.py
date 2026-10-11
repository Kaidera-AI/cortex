"""Verify every current PR40 publication member against its canonical root byte source."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
wt = Path(sys.argv[2])
index = Path(sys.argv[3])
data = json.loads(index.read_text())
for relative, member in data['members'].items():
    published = (wt / relative).read_bytes()
    canonical = (root / member['canonical_root']).read_bytes()
    assert len(published) == member['bytes'], ('size', relative)
    assert hashlib.sha256(published).hexdigest() == member['sha256'], ('published_digest', relative)
    assert hashlib.sha256(canonical).hexdigest() == member['sha256'], ('canonical_digest', relative)
print(json.dumps({'members': len(data['members']),
                  'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                  'all_members_match': True}))
