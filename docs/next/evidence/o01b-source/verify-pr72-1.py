"""Bind O01B-PR72-1 drift controls and renewed native proof to Git bytes."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys


wt = Path(sys.argv[1]).resolve()
evidence = wt / 'docs/next/evidence/o01b-source'
head = subprocess.check_output(['git', '-C', str(wt), 'rev-parse', 'HEAD'], text=True).strip()
native = json.loads((evidence / 'native-pg-pr72-1-001.json').read_bytes())
assert native['tree'] == 'aed5a1045aa3134fe274cf17840b2c40e8b949c0'
assert subprocess.run(['git', '-C', str(wt), 'merge-base', '--is-ancestor',
                       native['tree'], head]).returncode == 0


def digest(body):
    return hashlib.sha256(body).hexdigest()


def committed(path, ref=head):
    return subprocess.check_output(['git', '-C', str(wt), 'show', ref + ':' + path])


paths = ['native-pg-pr72-1-001.json', 'postcheck-native-preflight.json',
         'postcheck-drift-red.json', 'postcheck-drift-green-backup_producer.json',
         'postcheck-drift-green-backup_validation.json', 'postcheck-drift-mutant.json',
         'verify-pr72-1.py']
for name in paths:
    path = 'docs/next/evidence/o01b-source/' + name
    assert (wt / path).read_bytes() == committed(path), ('uncommitted_evidence', path)
assert native['passed'] and not native['errors']
for path, sha in native['source_sha256'].items():
    assert digest(committed(path, native['tree'])) == sha, ('tested_source', path)
    assert digest(committed(path)) == sha, ('current_source', path)
    assert digest((wt / path).read_bytes()) == sha, ('working_source', path)


def receipt(name, exit_code, count, expected_failures):
    data = json.loads((evidence / ('postcheck-drift-' + name + '.json')).read_bytes())
    report = data['receipt']
    assert data['exit_code'] == exit_code and report['tests_run'] == count
    assert not report['errors'] and len(report['failures']) == len(expected_failures)
    assert {row['id'] for row in report['failures']} == expected_failures
    assert all(row['phase'] == 'test' and row['is_assertion'] and
               row['phase_attribution'] == 'traceback+active-unittest-v2'
               for row in report['failures'])
    return data


failure_ids = {
    'test_validation.ValidationTests.test_base_file_changed_after_native_validation_cannot_publish',
    'test_validation.ValidationTests.test_wal_file_changed_after_native_validation_cannot_publish',
}
red = receipt('red', 1, 16, failure_ids)
green_validation = receipt('green-backup_validation', 0, 16, set())
green_producer = receipt('green-backup_producer', 0, 7, set())
mutant = receipt('mutant', 1, 16, failure_ids)
source = native['source_sha256']
assert red['head_before_test'] == '1e44f64d373806019e98fe02265e3368b301b940'
assert red['test_sha256'] == source['next/tests/backup_validation/test_validation.py']
for green in (green_validation, green_producer):
    assert green['source_sha256'] == {
        name: source['next/src/cortex_core/' + name]
        for name in ('backup.py', 'backup_validation.py')}
assert green_validation['test_sha256'] == source['next/tests/backup_validation/test_validation.py']
assert green_producer['test_sha256'] == source['next/tests/backup_producer/test_manifest.py']
assert mutant['mutation'] == 'remove sealing digest drift guard'
assert mutant['source_sha256'] == source['next/src/cortex_core/backup.py']

preflight = json.loads((evidence / 'postcheck-native-preflight.json').read_bytes())
assert preflight['mac_free_percent'] >= 35 and preflight['vm_free_percent'] >= 35
assert preflight['owned_name_absent'] and preflight['cpus'] == 2
assert preflight['memory_bytes'] == 1073741824 and preflight['network'] == 'none'
assert not preflight['ports'] and not preflight['bind_mounts']
assert preflight['postgres_image_id'] == 'db676a0ed906c00f55020fb8999e4fb30c598bf5c3b5c188630aef2812d3f11d'

state = native['native']
assert state['pg_version'] == '18.4' and state['segment_bytes'] == 16777216
assert state['before'] == [1, 1, 1, 0]
assert state['after_source'] == state['after_recovery'] == [2, 2, 2, 1]
assert state['mutation_overlapped_backup']
assert state['replay_lsn'] == state['archive_end_lsn'] != state['requested_end_lsn']
assert len(state['wal_segments']) == 4 and state['base_files'] == 1112
assert state['blob_members'] == 2 and state['verified_member_count'] == 1119
for key in ('postcheck_base_refused', 'postcheck_wal_refused', 'postcheck_no_ciphertext',
            'mixed_snapshot_refused', 'missing_blob_refused', 'altered_vector_refused',
            'altered_schema_refused', 'altered_wal_refused', 'missing_identity_refused',
            'bad_manifest_digest_refused'):
    assert state[key], key
for suite, count in (('backup_producer', 7), ('backup_validation', 16)):
    result = next(row for row in native['results'] if row['command'] == ['test_receipts', suite])
    report = json.loads(next(line.split('=', 1)[1] for line in result['stdout'].splitlines()
                             if line.startswith('CORTEX_TEST_RESULT=')))
    assert result['exit_code'] == 0 and report == {'tests_run': count, 'failures': [], 'errors': []}
assert native['limits'] == {'cpus': 2, 'memory': '1g'}
assert native['synthetic_only'] and not native['published_ports'] and not native['bind_mounts']
life = native['cleanup']
assert life['cleanup_verified'] and life['password_discarded'] and life['lock_released']
assert not life['pending'] and not life['owned']
created = {(row['kind'], row['name'], row['id']) for row in life['events']
           if row['event'] == 'created'}
removed = {(row['kind'], row['name'], row['id']) for row in life['events']
           if row['event'] == 'removed'}
assert len(created) == 1 and created == removed

print(json.dumps({'head': head, 'tested_tree': native['tree'],
                  'native_sha256': digest((evidence / 'native-pg-pr72-1-001.json').read_bytes()),
                  'focused_tests': [7, 16], 'red_body_failures': 2,
                  'mutant_body_failures': 2, 'postcheck_refusals': 2,
                  'owned_removed': 1}, sort_keys=True))
