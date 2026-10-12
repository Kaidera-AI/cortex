"""O01a source-copy faults; only expected test-body assertions count as kills."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path.cwd()
SOURCE = ROOT/'next/src/cortex_core/backup.py'
TESTS = ROOT/'next/tests/backup_producer'
RUNNER = ROOT/'next/tests/test_receipts.py'
original = SOURCE.read_text()
original_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
cases = [
    ('basebackup_wal_mode', '"-Fp", "-X", "none",', '"-Fp", "-X", "stream",',
     'test_manifest.BackupManifestTests.test_pg_basebackup_uses_plain_files_without_embedded_wal_or_secret'),
    ('interval_guard',
     'if not 0 < start < backup_end < archive_end or archive_end > (1 << 64) - 1:',
     'if not 0 < start < backup_end or archive_end > (1 << 64) - 1:',
     'test_manifest.BackupManifestTests.test_interval_not_beyond_backup_end_is_refused'),
    ('wal_continuity', 'for number in range(first, last + 1):',
     'for number in (first, last):',
     'test_manifest.BackupManifestTests.test_gap_inside_archive_interval_is_refused'),
    ('identity_binding', '"metadata": json.loads(canonical_metadata),', '"metadata": {},',
     'test_manifest.BackupManifestTests.test_complete_set_binds_metadata_and_exact_wal_digests'),
    ('base_file_binding', 'for path in sorted(base.rglob("*")):', 'for path in []:',
     'test_manifest.BackupManifestTests.test_complete_set_binds_metadata_and_exact_wal_digests'),
    ('encrypted_output', '    destination = Path(destination)\n',
     '    return manifest\n    destination = Path(destination)\n',
     'test_manifest.BackupManifestTests.test_complete_set_is_sealed_with_synthetic_age_key'),
]
cases.append(('caller_metadata_snapshot', '"metadata": json.loads(canonical_metadata),', '"metadata": metadata,', 'test_manifest.BackupManifestTests.test_caller_metadata_mutation_cannot_change_encrypted_manifest'))
rows = []
def clean_baseline():
    result = subprocess.run(['python3.12', str(RUNNER), str(TESTS)],
                            capture_output=True, text=True,
                            env={**os.environ, 'PYTHONPATH': str(ROOT/'next/src'),
                                 'PYTHONDONTWRITEBYTECODE': '1'})
    lines = [line.split('=', 1)[1] for line in result.stdout.splitlines()
             if line.startswith('CORTEX_TEST_RESULT=')]
    assert result.returncode == 0 and len(lines) == 1
    value = json.loads(lines[0])
    assert value == {'tests_run': 7, 'failures': [], 'errors': []}
    return {'exit_code': result.returncode, 'stdout': result.stdout,
            'stderr': result.stderr, 'receipt': value}

baseline = clean_baseline()
for name, before, after, expected in cases:
    assert original.count(before) == 1, name
    with tempfile.TemporaryDirectory(prefix='cox-o01a-fault-') as folder:
        replacement = Path(folder)/'cortex_core/backup.py'
        replacement.parent.mkdir()
        replacement.write_text(original.replace(before, after, 1))
        environment = os.environ.copy()
        environment['PYTHONPATH'] = folder+os.pathsep+str(ROOT/'next/src')
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        selected = name == 'encrypted_output'
        command = ['python3.12', str(RUNNER), str(TESTS)]
        if selected:
            script = '''import sys,json,unittest
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
from test_receipts import AssertionResult,MARKER
tests=unittest.defaultTestLoader.loadTestsFromName(sys.argv[3])
result=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(tests)
value={'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':test.id(),'traceback':trace} for test,trace in result.errors]}
print(MARKER+json.dumps(value));raise SystemExit(0 if result.wasSuccessful() else 1)
'''
            command = ['python3.12', '-c', script, str(ROOT/'next/tests'), str(TESTS), expected]
        result = subprocess.run(command,
                                capture_output=True, text=True, env=environment)
        lines = [line.split('=', 1)[1] for line in result.stdout.splitlines()
                 if line.startswith('CORTEX_TEST_RESULT=')]
        receipt = json.loads(lines[0]) if len(lines) == 1 else None
        failures = receipt['failures'] if receipt else []
        killed = (result.returncode == 1 and receipt is not None and
                  receipt['tests_run'] == (1 if selected else 7) and not receipt['errors'] and
                  any(f['id'] == expected for f in failures) and
                  all(f['phase'] == 'test' and f['is_assertion'] and
                      f['phase_attribution'] == 'traceback+active-unittest-v2'
                      for f in failures))
        row = {'name': name, 'status': 'killed' if killed else 'inconclusive',
               'test_selection': 'declared expected test only' if selected else 'full seven-test suite',
               'expected_test': expected, 'exit_code': result.returncode,
               'receipt': receipt, 'stdout': result.stdout, 'stderr': result.stderr,
               'mutated_sha256': hashlib.sha256(replacement.read_bytes()).hexdigest()}
        rows.append(row)
        print(json.dumps({'name': name, 'status': row['status'],
                          'failures': len(failures),
                          'errors': len(receipt['errors']) if receipt else None}), flush=True)
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == original_sha
restored = clean_baseline()
print(json.dumps({'source_sha256': original_sha, 'mutants': len(rows),
                  'killed': sum(row['status'] == 'killed' for row in rows),
                  'inconclusive': [row['name'] for row in rows if row['status'] != 'killed'],
                  'rows': rows, 'baseline': baseline, 'restored': restored}), flush=True)
if any(row['status'] != 'killed' for row in rows):
    raise SystemExit(1)
