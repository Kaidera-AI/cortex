"""Execute the literal Dockerfile digest guard on public signed snapshot bytes.

Fixtures were verified separately with gpgv against the keyring extracted from
its existing digest-pinned base image. No apt, network or native image runs here.
"""
from pathlib import Path
import hashlib
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "packages/containers/pdf-worker/Dockerfile"
FIXTURES = Path(__file__).parent / "fixtures/r421-debian"
DIGESTS = {
    "trixie": "98b25b5cd185c59d34aa6e4c3e9b5b8f01bbe9d104fe2dcfbcd30dc0a14a59ed",
    "trixie-security": "19a20e3818e39ae271b114aa048a20d961b3f4604c013bed46c2e6c991d08dbb",
    "trixie-updates": "5fec4fee5b084bff5ce5a46137cb6347b11393a02b81d2eed2e084ed62295048",
}


def literal_guard(suite, target):
    source = DOCKERFILE.read_text()
    marker = f"set -- /var/lib/apt/lists/*_dists_{suite}_InRelease;"
    guard = source[source.index(marker):]
    guard = guard[:guard.index('sha256sum -c -;') + len('sha256sum -c -;')]
    guard = guard.replace('\\\n', ' ').replace(
        f"/var/lib/apt/lists/*_dists_{suite}_InRelease", '"$R421_FIXTURE"')
    import os
    return subprocess.run(['sh', '-ec', guard], env=dict(os.environ, R421_FIXTURE=str(target)),
                          text=True, capture_output=True)


@pytest.mark.parametrize("suite", list(DIGESTS))
def test_literal_build_guard_accepts_verified_snapshot(suite):
    fixture = FIXTURES / (suite + '.InRelease')
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == DIGESTS[suite]
    result = literal_guard(suite, fixture)
    assert result.returncode == 0, result.stdout + result.stderr


def test_updates_metadata_label_agrees_with_verified_bytes():
    label = re.search(r'io\.kaidera\.kos\.apt\.updates-inrelease\.sha256="([a-f0-9]{64})"',
                      DOCKERFILE.read_text())
    assert label and label.group(1) == DIGESTS['trixie-updates']


@pytest.mark.parametrize("mutation", ['append', 'truncate'])
def test_updates_literal_build_guard_still_refuses_changed_bytes(tmp_path, mutation):
    original = (FIXTURES / 'trixie-updates.InRelease').read_bytes()
    target = tmp_path / 'InRelease'
    target.write_bytes(original + b'changed' if mutation == 'append' else original[:-1])
    result = literal_guard('trixie-updates', target)
    assert result.returncode != 0
