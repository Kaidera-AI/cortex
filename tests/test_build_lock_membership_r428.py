"""Offline membership expectations from verified publisher/wheel receipts."""
import hashlib
import json
from pathlib import Path
import re
import pytest
ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / 'tests/fixtures/r428-build-lock-sweep.json'
RAW = RECEIPT.read_bytes()
assert hashlib.sha256(RAW).hexdigest() == '32bf1e75957f9fe41eccaf29e7853e6481b77571671d5b0156be88fed02746a5'
SWEEP = json.loads(RAW)

@pytest.mark.parametrize('row', SWEEP['lock_results'], ids=lambda row: row['package'])
def test_image_build_hash_lock_admits_verified_native_x86_64_wheel(row):
    text = (ROOT / row['lock']).read_text().replace('\\\n', ' ')
    line = next(line for line in text.splitlines() if line.startswith(row['package'] + '=='))
    actual = set(re.findall(r'--hash=sha256:([a-f0-9]{64})', line))
    assert set(row['required_sha256']) <= actual
    assert set(row['existing_sha256']) <= actual
    assert actual == set(row['existing_sha256']) | set(row['required_sha256'])
    assert line.split()[0] == row['package'] + '==' + row['version']

def test_receipt_sweeps_every_explicit_hash_lock_in_native_image_recipes():
    actual = set()
    for name in SWEEP['image_recipes']:
        raw = (ROOT / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == SWEEP['recipe_sha256'][name]
        source = raw.decode().replace('\\\n', ' ')
        for line in source.splitlines():
            if line.startswith('RUN ') and '--require-hashes' in line:
                actual.add(re.search(r'(?:-r|--requirement)\s+([a-zA-Z0-9_.-]+)', line).group(1))
    assert actual == set(SWEEP['lock_files'])
    assert actual == {row['lock'] for row in SWEEP['lock_results']}
    for row in SWEEP['lock_results']:
        wheel = row['verified_x86_64_wheel']
        assert wheel['measured_sha256'] == wheel['metadata_sha256']
        assert row['required_sha256'] == [wheel['measured_sha256']]
        assert wheel['yanked'] is False
