"""Independent Git-bound O01b source, RED, fault, recovery and cleanup check."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

wt = Path(sys.argv[1]).resolve()
out = wt/'docs/next/evidence/o01b-source'
sha = lambda body: hashlib.sha256(body).hexdigest()
raw = out/'native-pg-final-004.json'
data = json.loads(raw.read_bytes())
tested = '1b0ee6a699e96e72abdff9dc560a90f3b0d95790'
assert data['tree'] == tested and data['passed'] and not data['errors']
assert subprocess.run(['git', '-C', str(wt), 'merge-base', '--is-ancestor',
                       '8c2bcd93dd747b93d4ec15bb8380f89db4b73856', tested]).returncode == 0
for path, digest in data['source_sha256'].items():
    assert sha((wt/path).read_bytes()) == digest, path
    assert sha(subprocess.check_output(['git', '-C', str(wt), 'show', tested+':'+path])) == digest, path
red = json.loads((out/'o01b-red-001.json').read_text())
assert red['exit_code'] == 1 and red['receipt']['tests_run'] == 9
assert len(red['receipt']['failures']) == 9 and not red['receipt']['errors']
assert all(row['phase'] == 'test' and row['is_assertion'] for row in red['receipt']['failures'])
mutants = json.loads((out/'mutation-001.jsonl').read_text().splitlines()[-1])
assert mutants['mutants'] == mutants['killed'] == 8 and not mutants['inconclusive']
for name, digest in mutants['source_sha256'].items():
    assert digest == data['source_sha256']['next/src/cortex_core/'+name]
for row in mutants['rows']:
    receipt = row['receipt']
    assert row['status'] == 'killed' and row['exit_code'] == 1
    assert receipt['tests_run'] == 14 and not receipt['errors']
    assert row['expected_test'] in {failure['id'] for failure in receipt['failures']}
    assert all(failure['phase'] == 'test' and failure['is_assertion'] and
               failure['phase_attribution'] == 'traceback+active-unittest-v2'
               for failure in receipt['failures'])
inherited = json.loads((out/'inherited-o01a-mutation.jsonl').read_text().splitlines()[-1])
assert inherited['mutants'] == inherited['killed'] == 6 and not inherited['inconclusive']
assert inherited['source_sha256'] == data['source_sha256']['next/src/cortex_core/backup.py']
native = data['native']
assert native['pg_version'] == '18.4' and native['segment_bytes'] == 16777216
assert native['before'] == [1, 1, 1, 0]
assert native['after_source'] == native['after_recovery'] == [2, 2, 2, 1]
assert native['mutation_overlapped_backup']
assert native['replay_lsn'] == native['archive_end_lsn']
assert native['requested_end_lsn'] != native['archive_end_lsn']
assert len(native['wal_segments']) == 4 and native['base_files'] == 1112
assert native['blob_members'] == 2 and native['verified_member_count'] == 1119
for key in ('mixed_snapshot_refused', 'missing_blob_refused',
            'altered_vector_refused', 'altered_schema_refused',
            'altered_wal_refused', 'missing_identity_refused',
            'bad_manifest_digest_refused'):
    assert native[key]
assert data['limits'] == {'cpus': 2, 'memory': '1g'}
assert data['synthetic_only'] and not data['published_ports'] and not data['bind_mounts']
life = data['cleanup']
assert life['cleanup_verified'] and life['password_discarded'] and life['lock_released']
assert not life['pending'] and not life['owned']
created = {(x['kind'], x['name'], x['id']) for x in life['events'] if x['event'] == 'created'}
removed = {(x['kind'], x['name'], x['id']) for x in life['events'] if x['event'] == 'removed'}
assert len(created) == 1 and created == removed
print(json.dumps({'tested_commit': tested, 'raw_sha256': sha(raw.read_bytes()),
                  'red_body_failures': 9, 'green_controls': 14,
                  'faults_killed': 8, 'wal_segments': 4,
                  'base_files': 1112, 'verified_members': 1119,
                  'owned_removed': 1, 'restored': True}, indent=2))
