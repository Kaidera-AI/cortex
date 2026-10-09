"""Bounded fixture image, not the graph/model release build."""
import hashlib
import io
import json
import marshal
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile

ROOT = Path('/Users/amadmalik/DevVault/helix')
WT = ROOT / '.worktrees/cox-r426-graph-cleanup-20261010'
OUT = ROOT / 'output/Cortex/design55-r426/2026-10-10/graph-cleanup'
OUT.mkdir(parents=True, exist_ok=True)
PHASE = sys.argv[1]
TARGET = OUT / (PHASE + '.json')
assert not TARGET.exists()
CONTEXT = ROOT / 'tmp/cox-r426-graph-cleanup-20261010' / PHASE
CONTEXT.mkdir(parents=True, exist_ok=False)
IMAGE = 'docker.io/library/python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e'
TAG = 'localhost/kaidera-test-graph-cleanup:' + PHASE
ARCHIVE = CONTEXT / 'fixture.oci.tar'
GRAPH = WT / 'packages/containers/graph-worker'
HEAD = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=WT, text=True).strip()
SYSTEM = ['var/log/apt/history.log', 'var/log/apt/term.log', 'var/log/dpkg.log', 'var/cache/ldconfig/aux-cache']
USER = ['home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
        'home/kaidera/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db', 'tmp/.ses']
BUILDER = ['root/.cache/Microsoft/DeveloperTools/.onnxruntime/deviceid',
           'root/.cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db']
results = []
scan = None
image_created = False
build_attempted = False


def run(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    results.append({'command': argv, 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr})
    print(json.dumps({'command': argv, 'exit_code': r.returncode, 'output': (r.stdout + r.stderr)[-2000:]}), flush=True)
    return r


def checked(argv):
    r = run(argv)
    assert r.returncode == 0
    return r


def py(code):
    return '/usr/local/bin/python -I -c ' + shlex.quote('exec(' + repr(code) + ')')


source = (GRAPH / 'Dockerfile').read_text().replace(chr(92) + chr(10), '')
user = '0'
hooks = []
for line in source.splitlines():
    if line.startswith('FROM '):
        user = '0'
    elif line.startswith('USER '):
        user = line.split()[1].split(':')[0]
    elif line.startswith('RUN ') and 'finalize-build.py --cleanup-only' in line:
        part = next(x.strip() for x in line[4:].split('&&') if 'finalize-build.py --cleanup-only' in x)
        hooks.append((user, part[part.index('/usr/local/bin/python -I /usr/local/libexec/kaidera/finalize-build.py'):]))
builder = next(cmd for uid, cmd in hooks if '--builder-home' in cmd)
system = next(cmd for uid, cmd in hooks if uid == '0' and '--builder-home' not in cmd)
runtime = next(cmd for uid, cmd in hooks if uid == '10001')
compile_command = next(line[4:] for line in source.splitlines() if line.startswith('RUN PYTHONHASHSEED=0'))
setup = '''import os
from pathlib import Path
for name in %r:
 p=Path('/'+name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'created-in-root-run')
Path('/var/cache/ldconfig').chmod(0o700)
p=Path('/home/kaidera');p.mkdir(parents=True,exist_ok=True);os.chown(p,10001,10001)
p=Path('/opt/kaidera-qwen3');p.mkdir();(p/'model.onnx').write_bytes(b'fixture-weights')
p=Path('/opt/bcrg/lib/python3.13/site-packages/bin/jp.py');p.parent.mkdir(parents=True);p.write_text('VALUE=42\\n')
''' % (SYSTEM + BUILDER + ['tmp/.ses'])
root_check = '''import os,json
from pathlib import Path
assert os.getuid()==0
assert all(not Path('/'+name).exists() for name in %r)
assert Path('/var/cache/ldconfig').stat().st_mode&0o777==0o700
print('CREATING_ROOT_RUN='+json.dumps({'uid':os.getuid(),'removed':%r,'parent_mode':'0700'}))
''' % (SYSTEM + BUILDER + ['tmp/.ses'], SYSTEM + BUILDER + ['tmp/.ses'])
user_setup = '''import os
from pathlib import Path
assert os.getuid()==10001 and os.getgid()==10001
for name in %r:
 p=Path('/'+name);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'created-in-runtime-run')
''' % USER
user_check = '''import os,json
from pathlib import Path
assert os.getuid()==10001 and os.getgid()==10001
assert all(not Path('/'+name).exists() for name in %r)
try:Path('/var/cache/ldconfig/aux-cache').lstat()
except PermissionError:pass
else:raise AssertionError('root-only parent must remain inaccessible to UID10001')
assert Path('/opt/kaidera-qwen3/model.onnx').read_bytes()==b'fixture-weights'
print('CREATING_RUNTIME_RUN='+json.dumps({'uid':os.getuid(),'gid':os.getgid(),'removed':%r,'parent_inaccessible':True}))
''' % (USER, USER)

# Exact production hook argv; only expensive apt/model producers replaced by seeds.
if PHASE.startswith('red'):
    runtime = runtime.replace('--cleanup-scope runtime', '--cleanup-scope all')
dockerfile = '\n'.join([
    'FROM ' + IMAGE,
    'COPY finalize-build.py /usr/local/libexec/kaidera/finalize-build.py',
    'RUN ' + ' && '.join([py(setup), builder, system, py(root_check)]),
    'USER 10001:10001',
    'RUN ' + ' && '.join([py(user_setup), runtime, py(user_check)]),
    'USER 0',
    'RUN ' + compile_command,
    'USER 10001:10001',
    'LABEL owner="cox@helix" purpose="H-D449-layer-regression"',
    'CMD ["/usr/local/bin/python","-c","print(42)"]', ''])
(CONTEXT / 'Dockerfile').write_text(dockerfile)
shutil.copy2(GRAPH / 'finalize-build.py', CONTEXT / 'finalize-build.py')
base_info = json.loads(subprocess.check_output(['podman', 'image', 'inspect', IMAGE], text=True))[0]
base_layers = len(base_info['RootFS']['Layers'])
before_ids = set(subprocess.check_output(['podman', 'images', '-aq'], text=True).split())
try:
    assert subprocess.run(['podman', 'image', 'exists', TAG], capture_output=True).returncode == 1
    build_attempted = True
    r = run(['podman', 'build', '--pull=never', '--network=none', '--cpu-period=100000',
             '--cpu-quota=200000', '--memory=1g', '--memory-swap=1g',
             '--cap-drop=ALL', '--cap-add=CHOWN', '--no-cache', '--layers=true',
             '--force-rm', '--rm', '--format=oci', '--layer-label', 'owner=cox@helix',
             '--label', 'owner=cox@helix', '-t', TAG, str(CONTEXT)])
    if PHASE.startswith('red'):
        assert r.returncode != 0 and 'PermissionError' in r.stdout+r.stderr
        assert '/var/cache/ldconfig/aux-cache' in r.stdout+r.stderr
    else:
        assert r.returncode == 0
        image_created = True
        checked(['podman', 'save', '--format=oci-archive', '-o', str(ARCHIVE), TAG])
        with tarfile.open(ARCHIVE) as outer:
            def read(name):
                return outer.extractfile(name).read()
            index = json.loads(read('index.json'))
            manifest = json.loads(read('blobs/sha256/' + index['manifests'][0]['digest'].split(':')[1]))
            config = json.loads(read('blobs/sha256/' + manifest['config']['digest'].split(':')[1]))
            assert config['config']['User'] == '10001:10001'
            layer_rows = []
            forbidden = set(SYSTEM + USER + BUILDER)
            violations = []
            compiled = []
            for i, layer in enumerate(manifest['layers']):
                blob = read('blobs/sha256/' + layer['digest'].split(':')[1])
                assert 'sha256:'+hashlib.sha256(blob).hexdigest() == layer['digest']
                entries = []
                with tarfile.open(fileobj=io.BytesIO(blob)) as inner:
                    for entry in inner.getmembers():
                        name = entry.name.removeprefix('./')
                        if name in forbidden and entry.isfile():
                            entries.append(name)
                            if i >= base_layers:
                                violations.append({'layer': i, 'path': name})
                        if i >= base_layers and name.endswith('/bin/__pycache__/jp.cpython-313.pyc'):
                            data = inner.extractfile(entry).read()
                            flags = struct.unpack('<I', data[4:8])[0]
                            filename = marshal.loads(data[16:]).co_filename
                            assert flags == 3 and filename == '/opt/bcrg/lib/python3.13/site-packages/bin/jp.py'
                            compiled.append({'layer': i, 'path': name, 'flags': flags, 'co_filename': filename,
                                             'sha256': hashlib.sha256(data).hexdigest()})
                layer_rows.append({'layer': i, 'digest': layer['digest'], 'immutable_base': i < base_layers,
                                   'ruled_file_data': entries})
            assert not violations and len(compiled) == 1
            scan = {'base_layer_count': base_layers, 'layers': layer_rows, 'new_layer_byproduct_violations': violations,
                    'fixture_jp_bytecode': compiled, 'final_user': config['config']['User'],
                    'archive_sha256': hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()}
            print(json.dumps(scan), flush=True)
finally:
    if image_created:
        checked(['podman', 'rmi', TAG])
    # Remove only new images explicitly labeled by this one sequential fixture build.
    after = json.loads(subprocess.check_output(['podman', 'images', '--filter', 'label=owner=cox@helix', '--format', 'json'], text=True))
    for item in after:
        image_id = item['Id']
        if image_id not in before_ids and item.get('Names') in (None, [], ['<none>:<none>']):
            checked(['podman', 'rmi', image_id])
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=WT, text=True).strip() == HEAD
    TARGET.write_text(json.dumps({'phase': PHASE, 'tree': HEAD, 'fixture_only': True, 'full_graph_build': False,
                                 'base_image': IMAGE, 'base_arch': base_info['Architecture'],
                                 'production_hooks': {'builder': builder, 'system': system, 'runtime': runtime, 'compile': compile_command},
                                 'fixture_dockerfile': dockerfile, 'results': results, 'scan': scan,
                                 'source_sha256': {str(p.relative_to(WT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                                   for p in [GRAPH/'Dockerfile', GRAPH/'finalize-build.py']},
                                 'limits': {'cpus': 2, 'memory': '1g'}, 'ports': [], 'bind_mounts': [],
                                 'owned_final_image_removed': subprocess.run(['podman','image','exists',TAG],capture_output=True).returncode == 1}, indent=2)+'\n')
