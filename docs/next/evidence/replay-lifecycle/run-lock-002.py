"""Run the lock-002 controller proof only inside a copied arm64 Python fixture."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid


PHASE = sys.argv[1]
assert PHASE in ('lock-red-004', 'lock-green-003', 'lock-mutant-003')
OUT = Path(__file__).resolve().parent
ROOT = Path('/Users/amadmalik/DevVault/helix')
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
IMAGE = 'sha256:b4600cb7d697d46d382b9581a7c52fa76f32d19536309c5096994757e62f3700'
NONCE = uuid.uuid4().hex
NAME = 'kaidera-test-lock002-' + NONCE[:12]
SOURCE = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.glob('*.py')}
RESULTS = []
MUTATIONS = []
container_id = None
passed = False
cleanup_ok = False


def run(args, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    RESULTS.append({'command': args, 'exit_code': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr})
    print(json.dumps({'command': args[:6], 'exit_code': result.returncode,
                      'tail': (result.stdout + result.stderr)[-500:]}), flush=True)
    return result


def checked(args):
    result = run(args)
    assert result.returncode == 0, args
    return result


def test_receipt(container):
    result = run(['podman', 'exec', '--env', 'REPLAY_VARIANT=safe',
                  '--env', 'PYTHONDONTWRITEBYTECODE=1', container,
                  'python', '/tmp/test_receipts.py', '/tmp/proof'])
    prefix = 'CORTEX_TEST_RESULT='
    rows = [json.loads(line[len(prefix):]) for line in result.stdout.splitlines()
            if line.startswith(prefix)]
    assert len(rows) == 1
    return result, rows[0]


try:
    image = json.loads(checked(['podman', 'image', 'inspect', IMAGE]).stdout)
    assert len(image) == 1 and image[0]['Architecture'] == 'arm64'
    created = checked(['podman', 'run', '-d', '--name', NAME,
                       '--label', 'owner=cox@helix',
                       '--label', 'kaidera.cox.lifecycle='+NONCE,
                       '--network', 'none', '--cpus', '2', '--memory', '1g',
                       '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
                       '--security-opt=no-new-privileges',
                       '--tmpfs', '/tmp:rw,size=268435456,mode=1777',
                       IMAGE, 'sleep', 'infinity'])
    container_id = created.stdout.strip()
    assert len(container_id) == 64
    meta = json.loads(checked(['podman', 'container', 'inspect', container_id]).stdout)
    assert len(meta) == 1 and meta[0]['Id'] == container_id
    assert meta[0]['Name'].removeprefix('/') == NAME
    assert meta[0]['Config']['Labels']['owner'] == 'cox@helix'
    assert meta[0]['Config']['Labels']['kaidera.cox.lifecycle'] == NONCE
    checked(['podman', 'cp', str(OUT), container_id+':/tmp/proof'])
    checked(['podman', 'cp', str(ROOT/'output/Cortex/design55-r426/2026-10-10/graph-cleanup/test_receipts.py'),
             container_id+':/tmp/test_receipts.py'])
    result, receipt = test_receipt(container_id)
    expected = 'test_lock_barrier.LockBarrier.test_renamed_owned_id_with_foreign_original_name_blocks_admission'
    if PHASE == 'lock-red-004':
        assert result.returncode == 1 and receipt['tests_run'] == 25
        assert not receipt['errors'] and len(receipt['failures']) == 1
        failure = receipt['failures'][0]
        assert failure['id'] == expected and failure['phase'] == 'test' and failure['is_assertion']
    else:
        assert result.returncode == 0 and receipt['tests_run'] == 25
        assert not receipt['errors'] and not receipt['failures']
    if PHASE == 'lock-mutant-003':
        source = (OUT/'replay_lifecycle.py').read_text()
        before = ("        if (labels or {}).get('owner') != 'cox@helix' or (labels or {}).get('kaidera.cox.lifecycle') != self.lifecycle:\n"
                  "            if identity is not None and identity.returncode == 0:\n"
                  "                raise RuntimeError('bound immutable ID remains under another name')")
        after = ("        if (labels or {}).get('owner') != 'cox@helix' or (labels or {}).get('kaidera.cox.lifecycle') != self.lifecycle:\n"
                 "            if False:  # deliberate foreign-name bound-ID bypass\n"
                 "                raise RuntimeError('bound immutable ID remains under another name')")
        assert source.count(before) == 1
        changed = source.replace(before, after, 1)
        patch = ("from pathlib import Path;p=Path('/tmp/proof/replay_lifecycle.py');"
                 "assert p.read_text()==" + repr(source) + ";p.write_text(" + repr(changed) + ")")
        checked(['podman', 'exec', container_id, 'python', '-c', patch])
        fault, fault_receipt = test_receipt(container_id)
        assert fault.returncode == 1 and fault_receipt['tests_run'] == 25
        assert not fault_receipt['errors'] and len(fault_receipt['failures']) == 1
        failure = fault_receipt['failures'][0]
        assert failure['id'] == expected and failure['phase'] == 'test' and failure['is_assertion']
        MUTATIONS.append({'name': 'foreign-name-bound-id-required',
                          'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                          'mutated_sha256': hashlib.sha256(changed.encode()).hexdigest(),
                          'expected_body_assertion': expected})
        checked(['podman', 'cp', str(OUT/'replay_lifecycle.py'),
                 container_id+':/tmp/proof/replay_lifecycle.py'])
        restored, restored_receipt = test_receipt(container_id)
        assert restored.returncode == 0 and restored_receipt['tests_run'] == 25
        assert not restored_receipt['errors'] and not restored_receipt['failures']
    passed = True
finally:
    if container_id is not None:
        meta = json.loads(checked(['podman', 'container', 'inspect', container_id]).stdout)
        assert len(meta) == 1 and meta[0]['Id'] == container_id
        assert meta[0]['Name'].removeprefix('/') == NAME
        assert meta[0]['Config']['Labels']['owner'] == 'cox@helix'
        assert meta[0]['Config']['Labels']['kaidera.cox.lifecycle'] == NONCE
        checked(['podman', 'rm', '-f', container_id])
        absent_id = run(['podman', 'container', 'exists', container_id])
        absent_name = run(['podman', 'container', 'exists', NAME])
        cleanup_ok = absent_id.returncode == 1 and absent_name.returncode == 1
    TARGET.write_text(json.dumps({'phase': PHASE, 'passed': bool(passed and cleanup_ok),
                                  'source_sha256': SOURCE, 'results': RESULTS,
                                  'mutations': MUTATIONS, 'container_id': container_id,
                                  'container_removed': cleanup_ok,
                                  'limits': {'cpus': 2, 'memory': '1g'},
                                  'ports': [], 'bind_mounts': [],
                                  'controller_safety_only': True,
                                  'application_tests_executed': False}, indent=2) + '\n')
    assert cleanup_ok
