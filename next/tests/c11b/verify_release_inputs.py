"""Read-only released-source freeze check; source pin is not artifact attestation."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def main():
    fixture = json.loads((ROOT / 'contracts/c11b-released-clients.json').read_text())
    roots = {
        'cortex': Path(os.environ['C11B_CORTEX_REPO']).resolve(),
        'kos': Path(os.environ['C11B_KOS_REPO']).resolve(),
        'openkai': Path(os.environ['C11B_KOS_REPO']).resolve(),
    }
    result = {}
    for name, repo in roots.items():
        item = fixture[name]
        actual_commit = git(repo, 'rev-parse', item['tag'] + '^{}').decode().strip()
        assert actual_commit == item['commit'], (name, 'commit differs')
        hashes = {}
        for path, expected in item['sources'].items():
            raw = git(repo, 'show', item['tag'] + ':' + path)
            digest = hashlib.sha256(raw).hexdigest()
            assert digest == expected, (name, path, 'bytes differ')
            hashes[path] = digest
        result[name] = {'commit': actual_commit, 'sources': hashes}
    helper = git(roots['kos'], 'show', fixture['kos']['tag'] + ':.agents/scripts/_cortex_api.sh').decode()
    ordinary = helper.split('cortex_api_call() {', 1)[1].split('cortex_api_json() {', 1)[0]
    assert 'Authorization:' not in ordinary and 'Bearer' not in ordinary
    client = git(roots['openkai'], 'show', fixture['openkai']['tag'] + ':' +
                 'products/openkai/packages/coding-agent/src/openkai/cortex/client.ts').decode()
    assert 'if (this.token) headers.Authorization = `Bearer ${this.token}`;' in client
    print(json.dumps({'source_freeze': 'pass', 'pins': result,
                      'kos_ordinary_bearer': False, 'openkai_bearer': 'optional'}, sort_keys=True))


if __name__ == '__main__':
    main()
