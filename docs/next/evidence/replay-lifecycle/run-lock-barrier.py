"""Copy-only native PR40 lock proof. Host code orchestrates bounded Podman only."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from replay_lifecycle import Lifecycle

ROOT = Path('/Users/amadmalik/DevVault/helix')
OUT = Path(__file__).resolve().parent
PHASE = sys.argv[1]
assert PHASE in ('lock-red-001', 'lock-red-002', 'lock-green-001', 'lock-mutant-001')
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
NAME = 'kaidera-test-replay-lock-1'
IMAGE = 'sha256:b4600cb7d697d46d382b9581a7c52fa76f32d19536309c5096994757e62f3700'
SOURCE = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('*.py')}
RESULTS = []
passed = False


def run(args):
    value = subprocess.run(args, capture_output=True, text=True, timeout=90)
    RESULTS.append({'command': args, 'exit_code': value.returncode,
                    'stdout': value.stdout, 'stderr': value.stderr})
    print(json.dumps({'command': args[:5], 'exit_code': value.returncode,
                      'tail': (value.stdout + value.stderr)[-900:]}), flush=True)
    return value


def checked(args):
    value = run(args)
    assert value.returncode == 0
    return value


life = Lifecycle(ROOT, run)
try:
    inspected = checked(['podman', 'image', 'inspect', IMAGE])
    image = json.loads(inspected.stdout)
    assert len(image) == 1 and image[0]['Architecture'] == 'arm64'
    life.acquire()
    life.create('container', NAME,
                ['podman', 'run', '-d', '--name', NAME, *life.labels(),
                 '--network', 'none', '--cpus', '2', '--memory', '1g',
                 '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges',
                 '--tmpfs', '/tmp:rw,size=268435456,mode=1777',
                 IMAGE, 'sleep', 'infinity'])
    checked(['podman', 'cp', str(OUT), NAME + ':/tmp/proof'])
    checked(['podman', 'cp', str(ROOT/'output/Cortex/design55-r426/2026-10-10/graph-cleanup/test_receipts.py'),
             NAME + ':/tmp/test_receipts.py'])
    if PHASE == 'lock-mutant-001':
        source = (OUT/'replay_lifecycle.py').read_text()
        before = '            found = self._read_marker()\n            if found is not None:'
        assert source.count(before) == 1
        changed = source.replace(before, '            found = None  # deliberate dirty barrier omission\n            if found is not None:', 1)
        mutation = "from pathlib import Path;p=Path('/tmp/proof/replay_lifecycle.py');assert p.read_text()==" + repr(source) + ";p.write_text(" + repr(changed) + ")"
        checked(['podman', 'exec', NAME, 'python', '-c', mutation])
    test_command = ['podman', 'exec', '--env', 'REPLAY_VARIANT=safe',
                '--env', 'PYTHONDONTWRITEBYTECODE=1', NAME,
                'python', '/tmp/test_receipts.py', '/tmp/proof']
    test = run(test_command)
    marker = 'CORTEX_TEST_RESULT='
    rows = [json.loads(line[len(marker):]) for line in test.stdout.splitlines() if line.startswith(marker)]
    assert len(rows) == 1
    receipt = rows[0]
    failures = receipt['failures']
    expected = 'test_lock_barrier.LockBarrier.test_failed_cleanup_blocks_competing_admission_until_exact_absence'
    if PHASE.startswith('lock-red') or PHASE == 'lock-mutant-001':
        assert test.returncode == 1 and receipt['tests_run'] == 22 and not receipt['errors']
        assert len(failures) == 1 and failures[0]['id'] == expected
        assert failures[0]['phase'] == 'test' and failures[0]['is_assertion']
    else:
        assert test.returncode == 0 and receipt['tests_run'] == 22 and not receipt['errors'] and not failures
    if PHASE == 'lock-mutant-001':
        checked(['podman', 'cp', str(OUT/'replay_lifecycle.py'), NAME + ':/tmp/proof/replay_lifecycle.py'])
        restored = run(test_command)
        restored_rows = [json.loads(line[len(marker):]) for line in restored.stdout.splitlines() if line.startswith(marker)]
        assert restored.returncode == 0 and len(restored_rows) == 1
        assert restored_rows[0]['tests_run'] == 22 and not restored_rows[0]['errors'] and not restored_rows[0]['failures']
    passed = True
finally:
    error = life.finish()
    gone = subprocess.run(['podman', 'container', 'exists', NAME], capture_output=True, timeout=30).returncode == 1
    TARGET.write_text(json.dumps({'phase': PHASE, 'passed': bool(passed and life.cleanup_verified and gone),
                                  'source_sha256': SOURCE, 'results': RESULTS,
                                  'cleanup': life.receipt(), 'container_removed': gone,
                                  'limits': {'cpus': 2, 'memory': '1g'}, 'ports': [], 'bind_mounts': [],
                                  'controller_safety_only': True, 'application_tests_executed': False},
                                 indent=2) + '\n')
    if error:
        raise error
    assert gone
