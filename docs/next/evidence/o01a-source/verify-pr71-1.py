"""Bind Mike's O01A-PR71-1 RED/GREEN/mutant proof to this Git head."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys


wt = Path(sys.argv[1]).resolve()
evidence = wt / 'docs/next/evidence/o01a-source'
head = subprocess.check_output(['git', '-C', str(wt), 'rev-parse', 'HEAD'], text=True).strip()
base = '8c2bcd93dd747b93d4ec15bb8380f89db4b73856'
assert subprocess.run(['git', '-C', str(wt), 'merge-base', '--is-ancestor', base, head]).returncode == 0


def digest(body):
    return hashlib.sha256(body).hexdigest()


def committed(path, ref=head):
    return subprocess.check_output(['git', '-C', str(wt), 'show', ref + ':' + path])


source = 'next/src/cortex_core/backup.py'
test = 'next/tests/backup_producer/test_manifest.py'
for path in (source, test, 'docs/next/evidence/o01a-source/verify-pr71-1.py'):
    assert (wt / path).read_bytes() == committed(path), ('uncommitted_source', path)
source_sha = digest(committed(source))
test_sha = digest(committed(test))
expected_test = 'test_manifest.BackupManifestTests.test_caller_metadata_mutation_cannot_change_encrypted_manifest'


def receipt(name, exit_code, failures):
    path = 'docs/next/evidence/o01a-source/metadata-mutation-' + name + '.json'
    raw = (wt / path).read_bytes()
    assert raw == committed(path), ('uncommitted_receipt', path)
    value = json.loads(raw)
    report = value['receipt']
    assert value['exit_code'] == exit_code and report['tests_run'] == 7
    assert not report['errors'] and len(report['failures']) == failures
    if failures:
        failure = report['failures'][0]
        assert failure['id'] == expected_test
        assert failure['phase'] == 'test' and failure['is_assertion']
        assert failure['phase_attribution'] == 'traceback+active-unittest-v2'
    return value, digest(raw)


red, red_sha = receipt('red', 1, 1)
green, green_sha = receipt('green', 0, 0)
mutant, mutant_sha = receipt('mutant', 1, 1)
assert red['source_head'] == base and red['test_sha256'] == test_sha
assert green['source_sha256'] == source_sha and green['test_sha256'] == test_sha
assert mutant['source_sha256'] == source_sha
assert mutant['mutation'] == 'restore caller-owned metadata assignment'

# The earlier native PostgreSQL proof remains historical at its tested source.
native = json.loads((evidence / 'native-pg-final-003.json').read_bytes())
assert native['passed'] and not native['errors'] and native['local_tests'] == 6
assert native['cleanup']['cleanup_verified']
for path, sha in native['source_sha256'].items():
    assert digest(committed(path, native['tree'])) == sha, ('historical_source', path)

print(json.dumps({'head': head, 'source_sha256': source_sha, 'test_sha256': test_sha,
                  'red_sha256': red_sha, 'green_sha256': green_sha,
                  'mutant_sha256': mutant_sha, 'body_failures': 1,
                  'focused_tests': 7, 'historical_native_bound': True}, sort_keys=True))
