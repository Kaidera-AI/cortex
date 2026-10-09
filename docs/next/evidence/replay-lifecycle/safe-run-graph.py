from replay_lifecycle import Lifecycle
"""Host orchestration only; all regression execution is inside bounded Podman."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path('/Users/amadmalik/DevVault/helix')
WT = ROOT / '.worktrees/cox-r426-graph-cleanup-20261010'
OUT = ROOT / 'output/Cortex/design55-r426/2026-10-10/graph-cleanup'
OUT.mkdir(parents=True, exist_ok=True)  # Check/create before any resource launch.
PHASE = sys.argv[1]
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists(), 'never overwrite an earlier attempt'
IMAGE = 'docker.io/library/python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e'
NAME = 'kaidera-test-graph-cleanup-1'
GRAPH = WT / 'packages/containers/graph-worker'
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=WT, text=True).strip()
results = []
passed = False
count = 8 if (GRAPH / 'tests/test_finalizer_guard.py').exists() else 7


def run(command):
    value = subprocess.run(command, capture_output=True, text=True, timeout=180)
    results.append({'command': command, 'exit_code': value.returncode,
                    'stdout': value.stdout, 'stderr': value.stderr})
    print(json.dumps({'command': command, 'exit_code': value.returncode,
                      'output': (value.stdout + value.stderr)[-2400:]}), flush=True)
    return value


def checked(command):
    value = run(command)
    assert value.returncode == 0, command
    return value


def report(value):
    marker = 'CORTEX_TEST_RESULT='
    records = [json.loads(line[len(marker):]) for line in value.stdout.splitlines()
               if line.startswith(marker)]
    assert len(records) == 1
    return records[0]


def clean(value):
    r = report(value)
    assert value.returncode == 0 and r['tests_run'] == count and not r['failures'] and not r['errors']


def killed(value, expected):
    r = report(value)
    assert value.returncode == 1 and r['tests_run'] == count and not r['errors']
    assert {row['id'] for row in r['failures']} == expected
    assert all(row['phase'] == 'test' and row['is_assertion'] is True for row in r['failures'])
    return r


BOOTSTRAP = r'''
import json,os,shlex,subprocess
from pathlib import Path
p=Path('/var/cache/ldconfig');p.mkdir(parents=True,exist_ok=True);p.chmod(0o700)
system=['/var/log/apt/history.log','/var/log/apt/term.log','/var/log/dpkg.log','/var/cache/ldconfig/aux-cache']
for name in system:
 p=Path(name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'root-build-byproduct')
source=Path('/proof/graph/Dockerfile').read_text().replace(chr(92)+chr(10),'')
line=next(x for x in source.splitlines() if x.startswith('RUN ') and 'apt-get update;' in x)
command=line[line.index('/usr/local/bin/python -I /usr/local/libexec/kaidera/finalize-build.py'):].strip()
argv=shlex.split(command);argv=[('/proof/graph/finalize-build.py' if x.endswith('/finalize-build.py') else x) for x in argv]
result=subprocess.run(argv,capture_output=True,text=True);print(result.stdout,end='');print(result.stderr,end='')
assert result.returncode==0
assert all(not Path(x).exists() for x in system)
Path('/proof/system-cleanup.json').write_text(json.dumps({'uid':os.getuid(),'command':argv,'all_system_byproducts_absent':True,'root_only_parent_mode':oct(Path('/var/cache/ldconfig').stat().st_mode&0o777)}))
home=Path('/home/kaidera');home.mkdir();os.chown(home,10001,10001)
model=Path('/opt/kaidera-qwen3');model.mkdir();(model/'model.onnx').write_bytes(b'fixture-weights')
'''
SUITE = r'''
import json,sys,unittest
sys.path.insert(0,'/proof')
from test_receipts import AssertionResult,MARKER
loader=unittest.TestLoader()
tests=unittest.TestSuite([loader.discover('/proof/graph/tests',pattern='test_finalize_build.py'),loader.discover('/proof/graph/tests',pattern='test_cleanup_ownership.py'),loader.discover('/proof/graph/tests',pattern='test_finalizer_guard.py')])
result=unittest.TextTestRunner(verbosity=2,resultclass=AssertionResult).run(tests)
print(MARKER+json.dumps({'tests_run':result.testsRun,'failures':result.assertions,'errors':[{'id':test.id(),'traceback':tb} for test,tb in result.errors]}),flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''
life = Lifecycle(ROOT, run)
try:
    life.acquire()
    assert subprocess.run(['podman', 'container', 'exists', NAME], capture_output=True).returncode == 1
    life.create('container', NAME, ['podman', 'run', '-d', '--name', NAME, *life.labels(),
             '--label', 'purpose=H-D449-regression', '--network', 'none', '--cpus', '2', '--memory', '1g',
             '--user', '0:0', '--read-only', '--cap-drop=ALL', '--cap-add=CHOWN',
             '--security-opt=no-new-privileges', '--image-volume=ignore',
             '--tmpfs', '/proof:rw,size=33554432,mode=0755',
             '--tmpfs', '/tmp:rw,size=67108864,mode=1777',
             '--tmpfs', '/var/cache:rw,size=16777216,mode=0755',
             '--tmpfs', '/var/log:rw,size=16777216,mode=0755',
             '--tmpfs', '/home:rw,size=1048576,mode=0755',
             '--tmpfs', '/opt:rw,size=1048576,mode=0755', IMAGE, 'sleep', 'infinity'])
    checked(['podman', 'cp', str(GRAPH), NAME + ':/proof/graph'])
    checked(['podman', 'cp', str(OUT / 'test_receipts.py'), NAME + ':/proof/test_receipts.py'])
    checked(['podman', 'exec', '--user', '0:0', NAME, 'python', '-I', '-c', BOOTSTRAP])
    checked(['podman', 'exec', '--user', '10001:10001', NAME, 'python', '-I', '-c',
             'import os,platform,json; from pathlib import Path; print(json.dumps({"uid":os.getuid(),"gid":os.getgid(),"python":platform.python_version(),"arch":platform.machine(),"capability_fields":[x for x in Path("/proc/self/status").read_text().splitlines() if x.startswith("Cap")]}))'])
    command = ['podman', 'exec', '--user', '10001:10001', NAME, 'python', '-I', '-c', SUITE]
    value = run(command)
    if PHASE.startswith('red'):
        r = killed(value, {
            'test_cleanup_ownership.OwnershipCleanupTests.test_real_runtime_uid_cleans_without_visiting_system_parent',
            'test_cleanup_ownership.OwnershipCleanupTests.test_creating_runs_select_their_own_paths_and_keep_hash_compile'})
        assert any('PermissionError' in row['traceback'] and '/var/cache/ldconfig/aux-cache' in row['traceback'] for row in r['failures'])
    else:
        clean(value)
        # Only Dockerfile runtime scope is reversed: actual child reaches inaccessible parent.
        patch = r'''from pathlib import Path
p=Path('/proof/graph/Dockerfile');s=p.read_text();a=s.index('USER 10001:10001');head,tail=s[:a],s[a:];assert tail.count('--cleanup-scope runtime')==1;p.write_text(head+tail.replace('--cleanup-scope runtime','--cleanup-scope all',1))'''
        checked(['podman', 'exec', '--user', '0:0', NAME, 'python', '-I', '-c', patch])
        mutant = run(command)
        killed(mutant, {
            'test_cleanup_ownership.OwnershipCleanupTests.test_real_runtime_uid_cleans_without_visiting_system_parent',
            'test_cleanup_ownership.OwnershipCleanupTests.test_creating_runs_select_their_own_paths_and_keep_hash_compile'})
        checked(['podman', 'cp', str(GRAPH / 'Dockerfile'), NAME + ':/proof/graph/Dockerfile'])
        # Classifier must expose real EACCES, never pretend the inaccessible file is absent.
        patch = r'''from pathlib import Path
p=Path('/proof/graph/finalize-build.py');s=p.read_text();before="        if path.is_symlink() or path.is_file():";assert s.count(before)==1;s=s.replace(before,"        if name == 'var/cache/ldconfig/aux-cache':\n            continue\n"+before);p.write_text(s)'''
        checked(['podman', 'exec', '--user', '0:0', NAME, 'python', '-I', '-c', patch])
        mutant = run(command)
        killed(mutant, {'test_finalize_build.FinalizerTests.test_cleanup_exact_paths_preserves_model_and_neighbor',
                        'test_cleanup_ownership.OwnershipCleanupTests.test_permission_errors_remain_visible_for_unscoped_cleanup'})
        checked(['podman', 'cp', str(GRAPH / 'finalize-build.py'), NAME + ':/proof/graph/finalize-build.py'])
        if count == 8:
            # The added final CLI guard is frozen before this actual omitted-call RED.
            patch = r'''from pathlib import Path
p=Path('/proof/graph/finalize-build.py');s=p.read_text();before="    if not args.cleanup_only:\n        verify_clean('/')";assert s.count(before)==1;p.write_text(s.replace(before,"    if False and not args.cleanup_only:\n        verify_clean('/')"))'''
            checked(['podman', 'exec', '--user', '0:0', NAME, 'python', '-I', '-c', patch])
            killed(run(command), {'test_finalizer_guard.FinalCompilationGuardTests.test_final_entrypoint_refuses_late_cleanup'})
            checked(['podman', 'cp', str(GRAPH / 'finalize-build.py'), NAME + ':/proof/graph/finalize-build.py'])
        clean(run(command))
        restored = checked(['podman', 'exec', '--user', '10001:10001', NAME, 'python', '-I', '-c',
                            'import hashlib,json;from pathlib import Path;p=Path("/proof/graph");print(json.dumps({str(x.relative_to(p)):hashlib.sha256(x.read_bytes()).hexdigest() for x in p.rglob("*") if x.is_file() and "__pycache__" not in x.parts}))'])
        restored_hashes = json.loads(restored.stdout)
        for name, digest in restored_hashes.items():
            assert hashlib.sha256((GRAPH / name).read_bytes()).hexdigest() == digest, name
    passed = True
finally:
    cleanup_error = life.finish()
    print('REPLAY_LIFECYCLE_RESULT='+json.dumps(life.receipt()), flush=True)
    removed = subprocess.run(['podman', 'container', 'exists', NAME], capture_output=True).returncode == 1
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=WT, text=True).strip() == HEAD
    source_hashes = {str(p.relative_to(WT)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in GRAPH.rglob('*') if p.is_file()}
    TARGET.write_text(json.dumps({'phase': PHASE, 'tree': HEAD, 'passed': passed, 'cleanup': life.receipt(), 'image': IMAGE, 'results': results,
                                 'source_sha256': source_hashes, 'limits': {'cpus': 2, 'memory': '1g'},
                                 'network': 'none', 'ports': [], 'bind_mounts': [],
                                 'container_removed': removed,
                                 'classifier_sha256': hashlib.sha256((OUT / 'test_receipts.py').read_bytes()).hexdigest()}, indent=2) + '\n')
    if cleanup_error:
        raise cleanup_error
    assert removed
