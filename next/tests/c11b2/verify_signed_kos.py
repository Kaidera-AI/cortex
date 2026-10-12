"""Verify the retained signed KOS v0.2.009 release and frozen client members."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

FIXTURE = Path(__file__).resolve().parents[2] / 'contracts/c11b2-signed-kos-fixture.json'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def verify(root):
    fixture = json.loads(FIXTURE.read_text())
    release = root / 'release.json'
    signature = root / 'release.json.minisig'
    assert digest(release) == fixture['release_sha256']
    assert digest(signature) == fixture['signature_sha256']
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / 'release.pub'
        key.write_text('untrusted comment: minisign public key 7BBB4B7E0CE8852E\n'
                       + fixture['public_key'] + '\n')
        result = subprocess.run(['minisign', '-Vm', str(release), '-p', str(key)],
                                capture_output=True, text=True, check=True)
    assert 'Signature and comment signature verified' in result.stdout
    manifest = json.loads(release.read_text())
    assert manifest['tag'] == fixture['release']
    assert manifest['source_revision'] == fixture['source_revision']
    assert {a['name']: [a['size'], a['sha256']] for a in manifest['artifacts']} == fixture['artifacts']
    for name, (size, expected) in fixture['artifacts'].items():
        path = root / name
        assert path.stat().st_size == size and digest(path) == expected, name
        if not name.endswith('.tar.zst'):
            continue
        for member, expected_member in fixture['archive_members'].items():
            archive_member = name.removesuffix('.tar.zst') + '/runtime/' + member
            content = subprocess.run(['tar', '--zstd', '-xOf', str(path), archive_member],
                                     capture_output=True, check=True).stdout
            assert hashlib.sha256(content).hexdigest() == expected_member, (name, member)
    print(json.dumps({'release': fixture['release'], 'signature': 'verified',
                      'artifacts': len(fixture['artifacts']),
                      'members_per_archive': len(fixture['archive_members'])}, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: verify_signed_kos.py ASSET_ROOT')
    verify(Path(sys.argv[1]))
