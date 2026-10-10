"""Stdlib host orchestration; copied native container owns all regression execution."""
import ast
import fcntl
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT = Path('/Users/amadmalik/DevVault/helix')
WT = Path.cwd()
GRAPH = WT / 'packages/containers/graph-worker'
OUT = ROOT / 'output/Cortex/design55-r426/2026-10-10/graph-pyc'
OUT.mkdir(parents=True, exist_ok=True)
PHASE = sys.argv[1]
subprocess.run([sys.executable, str(Path(__file__).parent/'verify-fixture-parser.py')], check=True)
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists(), 'append-only attempts'
TREE = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=WT, text=True).strip()
assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=WT, text=True)
SOURCE = {str(p.relative_to(GRAPH)): hashlib.sha256(p.read_bytes()).hexdigest() for p in GRAPH.rglob('*') if p.is_file()}
TOOLS = {str(p.relative_to(WT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
IMAGE = 'docker.io/library/python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e'
NAME = 'kaidera-test-graph-pyc-1'
NONCE = secrets.token_hex(16)
MARKER = ROOT / 'tmp/cox-graph-pyc-dirty.json'
results = []
lock = None
pending = False
bound = None
cleanup_verified = False
passed = False
cleanup_attempts = []


def run(args):
    value = subprocess.run(args, capture_output=True, text=True, timeout=90)
    results.append({'command': args, 'exit_code': value.returncode, 'stdout': value.stdout, 'stderr': value.stderr})
    print(json.dumps({'command': args[:5], 'exit_code': value.returncode, 'output': (value.stdout+value.stderr)[-1400:]}), flush=True)
    return value


def checked(args):
    value = run(args)
    assert value.returncode == 0, args[:4]
    return value


def receipt(value, red=False):
    rows = [json.loads(line.split('=', 1)[1]) for line in value.stdout.splitlines() if line.startswith('CORTEX_TEST_RESULT=')]
    assert len(rows) == 1
    row = rows[0]
    assert row['tests_run'] == 9 and not row['errors'], row
    expected = {'test_builder_pyc.BuilderBytecodeTests.test_builder_commits_only_checked_hash_venv_bytecode'} if red else set()
    assert value.returncode == (1 if red else 0)
    assert {r['id'] for r in row['failures']} == expected
    assert all(r['phase'] == 'test' and r['is_assertion'] for r in row['failures'])
    if red:
        assert all('builder still commits timestamp-mode pyc' in r['traceback'] for r in row['failures'])
    return row


def inspect():
    value = run(['podman', 'container', 'exists', NAME])
    if value.returncode == 1:
        if bound is not None:
            assert run(['podman', 'container', 'exists', bound]).returncode == 1, 'known immutable ID is still present'
        return None
    assert value.returncode == 0, 'existence unverified'
    row = json.loads(checked(['podman', 'container', 'inspect', NAME]).stdout)[0]
    labels = row['Config']['Labels']
    assert row['Name'].removeprefix('/') == NAME
    assert labels.get('owner') == 'cox@helix' and labels.get('kaidera.cox.lifecycle') == NONCE, 'foreign identity preserved'
    assert re.fullmatch('[0-9a-f]{64}', row['Id'])
    assert bound is None or bound == row['Id'], 'immutable identity changed'
    return row


# Parse frozen orchestration constants only; never import or execute host product code.
old = ast.parse((WT / 'docs/qualification/evidence/r426-graph-cleanup-20261010/run-graph.py').read_text())
constants = {n.targets[0].id: ast.literal_eval(n.value) for n in old.body if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ('BOOTSTRAP', 'SUITE')}
bootstrap = constants['BOOTSTRAP'] + "\np=Path('/opt/bcrg');p.mkdir();os.chown(p,10001,10001)\n"
suite = constants['SUITE'].replace("loader.discover('/proof/graph/tests',pattern='test_finalizer_guard.py')", "loader.discover('/proof/graph/tests',pattern='test_finalizer_guard.py'),loader.discover('/proof/graph/tests',pattern='test_builder_pyc.py')")
try:
    assert not MARKER.exists(), 'dirty previous lifecycle requires exact-identity reconciliation; producer held'
    candidate = (ROOT / 'tmp/cox-podman.lock').open('a')
    try:
        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        candidate.close()
        raise
    lock = candidate
    for kind, name in [('pod', 'kaidera-test-core-schema-1'), ('container', 'kaidera-test-core-db-1'), ('container', 'kaidera-test-core-driver-1'), ('container', NAME)]:
        assert run(['podman', kind, 'exists', name]).returncode == 1, 'prior cox fixture must be absent before launch'
    pending = True
    MARKER.write_text(json.dumps({'owner': 'cox@helix', 'lifecycle': NONCE, 'kind': 'container', 'name': NAME, 'pending': True, 'id': None})+'\n')
    MARKER.chmod(0o600)
    checked(['podman', 'run', '-d', '--name', NAME, '--label', 'owner=cox@helix', '--label', 'kaidera.cox.lifecycle='+NONCE,
             '--network', 'none', '--cpus', '2', '--memory', '1g', '--user', '0:0', '--read-only', '--cap-drop=ALL', '--cap-add=CHOWN',
             '--security-opt=no-new-privileges', '--image-volume=ignore', '--tmpfs', '/proof:rw,size=33554432,mode=0755',
             '--tmpfs', '/tmp:rw,size=67108864,mode=1777', '--tmpfs', '/var/cache:rw,size=16777216,mode=0755',
             '--tmpfs', '/var/log:rw,size=16777216,mode=0755', '--tmpfs', '/home:rw,size=1048576,mode=0755',
             '--tmpfs', '/opt:rw,size=8388608,mode=0755', IMAGE, 'sleep', 'infinity'])
    bound = inspect()['Id']
    MARKER.write_text(json.dumps({'owner': 'cox@helix', 'lifecycle': NONCE, 'kind': 'container', 'name': NAME, 'pending': True, 'id': bound})+'\n')
    checked(['podman', 'cp', str(GRAPH), bound+':/proof/graph'])
    checked(['podman', 'cp', str(WT/'docs/qualification/evidence/r426-graph-cleanup-20261010/test_receipts.py'), bound+':/proof/test_receipts.py'])
    checked(['podman', 'exec', '--user', '0:0', bound, 'python', '-I', '-c', bootstrap])
    identity = checked(['podman', 'exec', '--user', '10001:10001', bound, 'python', '-I', '-c', 'import os,platform,json;print(json.dumps({"uid":os.getuid(),"gid":os.getgid(),"arch":platform.machine(),"python":platform.python_version()}))'])
    metadata = json.loads(identity.stdout)
    assert metadata == {'uid':10001,'gid':10001,'arch':'aarch64','python':'3.13.15'}, metadata
    command = ['podman', 'exec', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--user', '10001:10001', bound, 'python', '-I', '-c', suite]
    receipt(run(command), red=PHASE.startswith('red'))
    if not PHASE.startswith('red'):
        patch = "from pathlib import Path\np=Path('/proof/graph/Dockerfile');s=p.read_text();lines=s.splitlines(keepends=True);hits=[x for x in lines if '-m compileall' in x];assert len(hits)==1;p.write_text(''.join(x for x in lines if x not in hits))"
        checked(['podman', 'exec', '--user', '0:0', bound, 'python', '-I', '-c', patch])
        receipt(run(command), red=True)
        checked(['podman', 'cp', str(GRAPH/'Dockerfile'), bound+':/proof/graph/Dockerfile'])
        receipt(run(command))
    restored = json.loads(checked(['podman', 'exec', '--user', '10001:10001', bound, 'python', '-I', '-c', "import hashlib,json;from pathlib import Path;p=Path('/proof/graph');print(json.dumps({str(x.relative_to(p)):hashlib.sha256(x.read_bytes()).hexdigest() for x in p.rglob('*') if x.is_file() and '__pycache__' not in x.parts}))"]).stdout)
    assert restored == SOURCE, 'copied graph inventory not restored'
    passed = True
finally:
    if lock is not None:
        for attempt in range(2):
            try:
                row = inspect() if pending else None
                if row is not None:
                    checked(['podman', 'rm', '-f', row['Id']])
                    assert inspect() is None
                cleanup_verified = True
                pending = False
                MARKER.unlink(missing_ok=True)
                cleanup_attempts.append({'attempt':attempt+1,'verified':True})
                break
            except Exception as error:
                cleanup_attempts.append({'attempt':attempt+1,'verified':False,'error':str(error)})
        if cleanup_verified:
            lock.close()
            lock = None
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=WT,text=True).strip() == TREE
    assert {str(p.relative_to(GRAPH)):hashlib.sha256(p.read_bytes()).hexdigest() for p in GRAPH.rglob('*') if p.is_file()} == SOURCE
    TARGET.write_text(json.dumps({'phase':PHASE,'tree':TREE,'passed':passed and cleanup_verified,'cleanup_verified':cleanup_verified,'pending':pending,'immutable_id':bound,'lifecycle':NONCE,'lock_released':lock is None,'cleanup_attempts':cleanup_attempts,'source_sha256':SOURCE,'tools_sha256':TOOLS,'image':IMAGE,'limits':{'cpus':2,'memory':'1g'},'ports':[],'bind_mounts':[],'network':'none','results':results},indent=2)+'\n')
assert passed and cleanup_verified
