"""Independent Git-bound O01a RED, native, mutation and cleanup verification."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
out = wt/'docs/next/evidence/o01a-source'
sha = lambda body: hashlib.sha256(body).hexdigest()
raw = out/'native-pg-final-003.json'
data = json.loads(raw.read_bytes())
tested = '7eb8eae10a00e5ef778267dcd6c44be51c538d39'
assert data['passed'] and not data['errors'] and data['tree'] == tested
assert subprocess.run(['git', '-C', str(wt), 'merge-base', '--is-ancestor',
                       'e2e5a889a47c7ee538dfead08f44c7773fd72fe0', data['tree']]).returncode == 0
for path, digest in data['source_sha256'].items():
    assert sha((wt/path).read_bytes()) == digest, path
    assert sha(subprocess.check_output(['git', '-C', str(wt), 'show', data['tree']+':'+path])) == digest, path
native = data['native']
assert native['pg_version'] == '18.4' and native['segment_bytes'] == 16777216
assert native['pg_verifybackup_passed'] and native['synthetic_age_roundtrip']
assert native['base_only_refused'] and native['gap_refused'] and native['unbound_interval_refused']
assert len(native['wal_segments']) >= 3 and native['base_files'] >= 3
def lsn(value):
    high, low = value.split('/')
    return (int(high, 16) << 32) | int(low, 16)
assert lsn(native['archive_end_lsn']) > lsn(native['backup_end_lsn'])
assert data['local_tests'] == 6 and data['limits'] == {'cpus': 2, 'memory': '1g'}
assert not data['published_ports'] and not data['bind_mounts'] and data['synthetic_only']
life = data['cleanup']
assert life['cleanup_verified'] and life['password_discarded'] and life['lock_released']
assert not life['pending'] and not life['owned']
created = {(x['kind'], x['name'], x['id']) for x in life['events'] if x['event'] == 'created'}
removed = {(x['kind'], x['name'], x['id']) for x in life['events'] if x['event'] == 'removed'}
assert len(created) == 1 and created == removed
for filename, tests, failures in [('o01a-red-001.json', 4, 4),
                                  ('o01a-encryption-red-001.json', 5, 1),
                                  ('o01a-command-red-001.json', 6, 1)]:
    red = json.loads((out/filename).read_text())
    receipt = red['receipt']
    assert red['exit_code'] == 1 and receipt['tests_run'] == tests
    assert len(receipt['failures']) == failures and not receipt['errors']
    assert all(x['phase'] == 'test' and x['is_assertion'] for x in receipt['failures'])
lines = (out/'mutation-002.jsonl').read_text().splitlines()
mutants = json.loads(lines[-1])
assert mutants['mutants'] == mutants['killed'] == 6 and not mutants['inconclusive']
assert mutants['source_sha256'] == data['source_sha256']['next/src/cortex_core/backup.py']
for row in mutants['rows']:
    receipt = row['receipt']
    assert row['status'] == 'killed' and row['exit_code'] == 1
    assert receipt['tests_run'] == 6 and not receipt['errors']
    assert row['expected_test'] in {x['id'] for x in receipt['failures']}
    assert all(x['phase'] == 'test' and x['is_assertion'] and
               x['phase_attribution'] == 'traceback+active-unittest-v2'
               for x in receipt['failures'])
print(json.dumps({'tested_commit': data['tree'], 'raw_sha256': sha(raw.read_bytes()),
                  'local_tests': 6, 'faults_killed': 6, 'wal_segments': len(native['wal_segments']),
                  'base_files': native['base_files'], 'red_body_failures': 6,
                  'owned_removed': len(removed), 'restored': True}, indent=2))
