"""O01b source-copy faults; only expected test-body assertions count."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT/'next/src/cortex_core'
TESTS = ROOT/'next/tests/backup_validation'
RUNNER = ROOT/'next/tests/test_receipts.py'
sources = {name: (CORE/name).read_text() for name in ('backup.py', 'backup_validation.py')}
hashes = {name: hashlib.sha256((CORE/name).read_bytes()).hexdigest() for name in sources}
cases = [
    ('replay_endpoint', 'backup_validation.py',
     'if _lsn(metadata["replay_lsn"]) != _lsn(archive_end_lsn):', 'if False:',
     'test_validation.ValidationTests.test_manifest_endpoint_must_equal_actual_replay_lsn'),
    ('mixed_snapshot', 'backup_validation.py',
     'if not isinstance(snapshot, dict) or snapshot != manifest["metadata"]:', 'if False:',
     'test_validation.ValidationTests.test_mixed_record_vector_blob_checkpoint_snapshot_is_refused'),
    ('member_set', 'backup_validation.py',
     'if expected is None or seen != set(expected) | {"manifest.json"}:',
     'if expected is None:',
     'test_validation.ValidationTests.test_missing_blob_is_refused'),
    ('member_digest', 'backup_validation.py',
     'if digest.hexdigest() != details["sha256"]:', 'if False:',
     'test_validation.ValidationTests.test_altered_blob_is_refused'),
    ('native_manifest_checksum', 'backup_validation.py',
     'hashlib.sha256(b"".join(lines[:-1])).hexdigest() != native.get("Manifest-Checksum")',
     'False', 'test_validation.ValidationTests.test_native_manifest_changed_before_seal_is_refused'),
    ('native_wal_at_seal', 'backup_validation.py',
     '    _validate_native_pg(Path(base_dir), Path(archive_dir), preliminary)\n', '',
     'test_validation.ValidationTests.test_native_wal_parser_failure_blocks_seal'),
    ('native_wal_at_verify', 'backup_validation.py',
     '        _validate_native_pg(Path(scratch.name)/"base", Path(scratch.name)/"wal", manifest)\n', '',
     'test_validation.ValidationTests.test_native_wal_parser_failure_refuses_recoverable'),
    ('blob_bytes_in_bundle', 'backup.py',
     'for path, name, entry in blobs:', 'for path, name, entry in []:',
     'test_validation.ValidationTests.test_complete_bundle_binds_blob_and_full_snapshot'),
]
rows = []
for name, filename, before, after, expected in cases:
    assert sources[filename].count(before) == 1, name
    with tempfile.TemporaryDirectory(prefix='cox-o01b-fault-') as folder:
        module = Path(folder)/'cortex_core'
        module.mkdir()
        for source_name, content in sources.items():
            (module/source_name).write_text(content.replace(before, after, 1)
                                            if source_name == filename else content)
        env = os.environ.copy()
        env['PYTHONPATH'] = folder+os.pathsep+str(ROOT/'next/src')
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run(['python3.12', str(RUNNER), str(TESTS)],
                                capture_output=True, text=True, env=env)
        lines = [line.split('=', 1)[1] for line in result.stdout.splitlines()
                 if line.startswith('CORTEX_TEST_RESULT=')]
        receipt = json.loads(lines[0]) if len(lines) == 1 else None
        failures = receipt['failures'] if receipt else []
        killed = (result.returncode == 1 and receipt is not None and
                  receipt['tests_run'] == 14 and not receipt['errors'] and
                  any(row['id'] == expected for row in failures) and
                  all(row['phase'] == 'test' and row['is_assertion'] and
                      row['phase_attribution'] == 'traceback+active-unittest-v2'
                      for row in failures))
        row = {'name': name, 'status': 'killed' if killed else 'inconclusive',
               'expected_test': expected, 'exit_code': result.returncode,
               'receipt': receipt, 'stdout': result.stdout, 'stderr': result.stderr,
               'mutated_sha256': hashlib.sha256((module/filename).read_bytes()).hexdigest()}
        rows.append(row)
        print(json.dumps({'name': name, 'status': row['status'],
                          'failures': len(failures),
                          'errors': len(receipt['errors']) if receipt else None}), flush=True)
assert all(hashlib.sha256((CORE/name).read_bytes()).hexdigest() == digest
           for name, digest in hashes.items())
print(json.dumps({'source_sha256': hashes, 'mutants': len(rows),
                  'killed': sum(row['status'] == 'killed' for row in rows),
                  'inconclusive': [row['name'] for row in rows if row['status'] != 'killed'],
                  'rows': rows}), flush=True)
if any(row['status'] != 'killed' for row in rows):
    raise SystemExit(1)
