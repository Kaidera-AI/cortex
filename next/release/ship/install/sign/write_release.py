#!/usr/bin/env python3
"""Write release.json + SHA256SUMS for ONE release folder (ship/sign, nix). The payload is every file in the folder; the index files and signatures are
never payload (a file cannot name its own digest, a signature cannot sit inside what it signs), so a folder that already holds release.json,
SHA256SUMS or any *.minisig is refused (exit 3) and nothing is written. The CTO signs afterwards: minisign -S -s KEY -m release.json SHA256SUMS.
usage: write_release.py --dir DIR --identity identity.json --images images.json"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

INDEX = ('release.json', 'SHA256SUMS')
IDENTITY = ('release_id', 'release_lineage', 'release_sequence', 'api_contract', 'source_revision')


def refuse(reason):
    print(json.dumps({'reason': reason, 'unit': 'write_release'}), file=sys.stderr)
    sys.exit(3)


def kind(name):
    if name.endswith('.oci.tar'):
        return 'image'
    if name.endswith('.sh'):
        return 'script'
    return 'contract' if name.endswith('.json') else 'file'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--identity', required=True)
    ap.add_argument('--images', required=True)
    a = ap.parse_args()
    d = Path(a.dir)
    entries = sorted(d.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in entries):
        refuse('payload_not_flat_regular_files')
    names = [p.name for p in entries]
    bad = [n for n in names if n in INDEX or n.endswith('.minisig')]
    if bad:
        refuse('payload_names_index_file:' + ','.join(bad))
    try:
        identity = json.loads(Path(a.identity).read_text())
        images = json.loads(Path(a.images).read_text())
        assert all(k in identity for k in IDENTITY) and isinstance(images, dict)
    except (OSError, ValueError, AssertionError):
        refuse('identity_or_images_invalid')
    files = []
    for p in entries:
        data = p.read_bytes()
        m = re.search(r'linux-(arm64|amd64)', p.name)
        files.append({'name': p.name, 'kind': kind(p.name), 'platform': f'linux/{m.group(1)}' if m else 'all',
                      'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
    doc = {'schema': 'cortex.release.v1', **{k: identity[k] for k in IDENTITY}, 'images': images, 'files': files}
    release = (json.dumps(doc, sort_keys=True, indent=1) + '\n').encode()
    sums = {f['name']: f['sha256'] for f in files}
    sums['release.json'] = hashlib.sha256(release).hexdigest()
    text = ''.join(f'{sums[n]}  {n}\n' for n in sorted(sums)).encode()
    for name, data in (('release.json', release), ('SHA256SUMS', text)):
        tmp = d / (name + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, d / name)


main()
